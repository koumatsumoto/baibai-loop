"""Run the local daily machine batch as one command.

Screening cache acquisition and its single Stock Analysis run publication are
independent of Market Context indicator refresh. A Japan holiday skips only
Screening. Validated L1 chunks remain committed after a later source failure.
The complete serving image is materialized once; unchanged Screening history
is retained. Domain failures are reported with exit 3 after the export, while an
unsafe/unreadable store or failed export remains fatal. Run exit 2 is a published
run with partial-quality warnings.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import closing, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_batch.jobs.schedule import batch_target_date
from baibai_batch.observability.discord import sanitize_one_line, write_json_atomic
from baibai_engine.batch_api import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    repository_root_error,
    validate_candidate_run,
)
from baibai_engine.read_api import market_calendar_business_day

_JST = ZoneInfo("Asia/Tokyo")
_ENGINE = "baibai-engine"
_MARKET_DB_RELPATH = MARKET_DB_PATH
_RUNS_DB_RELPATH = RUNS_DB_PATH
_EXPORT_MODULE = "baibai_web.materialize"
_STDERR_SUMMARY_LINES = 20
# Exit 3 means a complete serving image was exported with a domain failure.
# Screening may remain on its retained publication while indicators advance.
_EXIT_DEFERRED_FAILURE = 3
_EDINET_QUARANTINE_RE = re.compile(
    r"\bquarantined_events=(?P<events>\d+)\s+"
    r"quarantined_tickers=(?P<tickers>\d+)\s+"
    r"quarantine_sample=(?P<sample>[^\s;]+)"
)
# Bound company names while preserving ticker and estimate in each notice entry.
_DELTA_TICKER_NAME_MAX_CHARS = 24
_DELTA_TICKER_LABEL_MAX_CHARS = 48


class BatchStepError(RuntimeError):
    """A batch step failed or produced output the chain cannot continue from.

    ``stage`` names the step for the notification's "failed step" text. ``stderr``
    carries the step's unredacted output for a handler that has to read what the
    step reported; it never reaches the notification.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.returncode = returncode
        self.stderr = stderr


class CalendarCoverageError(RuntimeError):
    """The market calendar cannot answer the business-day question for a date."""

    stage = "calendar"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


type CommandRunner = Callable[[Sequence[str], Path], CommandResult]


@dataclass(frozen=True, slots=True)
class StepResult:
    """One observed public-CLI step without changing its business semantics."""

    name: str
    argv: tuple[str, ...]
    returncode: int
    duration_seconds: float

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 6),
        }


type StepSink = Callable[[StepResult, CommandResult], None]


@dataclass(frozen=True, slots=True)
class DailyBatchResult:
    """Machine-readable identity and outcome of the existing daily chain."""

    status: str
    asof: str
    exit_code: int
    run_revision_id: str | None = None
    candidate_count: int | None = None
    deferred_failure_count: int = 0
    steps: tuple[StepResult, ...] = ()
    screening_status: str = "published"
    market_updated: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "asof": self.asof,
            "exit_code": self.exit_code,
            "run_revision_id": self.run_revision_id,
            "candidate_count": self.candidate_count,
            "deferred_failure_count": self.deferred_failure_count,
            "steps": [step.to_json() for step in self.steps],
            "screening_status": self.screening_status,
            "market_updated": self.market_updated,
        }


def _run_subprocess(argv: Sequence[str], cwd: Path) -> CommandResult:
    # Fixed argv list, shell=False: the command line never passes through a shell.
    completed = subprocess.run(  # nosec B603
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _require_business_day(market_db: Path, day: date) -> bool:
    """Answer from the market calendar; raise when the calendar cannot answer.

    The calendar read lives in ``baibai_engine.read_api`` so it is drift-guarded
    with the store schema. A missing file, an uncovered date, and a corrupt store
    each stop Screening with a distinct message; independent indicators continue.
    """

    if not market_db.is_file():
        # failure policy: 3 — Screening has no required calendar store.
        raise CalendarCoverageError(f"market SQLite does not exist: {market_db}")
    try:
        result = market_calendar_business_day(market_db, day)
    except sqlite3.Error as exc:
        # failure policy: 3 — an unreadable calendar cannot supply the required row.
        raise CalendarCoverageError(f"market calendar is unreadable in {market_db}: {exc}") from exc
    if result is None:
        # failure policy: 3 — the calendar has no row for the target date.
        raise CalendarCoverageError(
            f"market calendar does not cover {day.isoformat()}; "
            "refresh the calendar cache before running the daily batch",
        )
    return result


def _run_step(
    runner: CommandRunner,
    *,
    name: str,
    argv: Sequence[str],
    cwd: Path,
    allowed_exit_codes: Sequence[int] = (0,),
    echo_stdout: bool = True,
    echo_stdout_prefixes: Sequence[str] = (),
    quiet: bool = False,
    step_sink: StepSink | None = None,
) -> CommandResult:
    if not quiet:
        print(f"$ {' '.join(argv)}", flush=True)
    started = time.monotonic()
    try:
        result = runner(argv, cwd)
    except FileNotFoundError as exc:
        # failure policy: 1 — the executable required by this batch step is absent.
        raise BatchStepError(
            f"step {name}: command not found: {argv[0]}",
            stage=name,
        ) from exc
    elapsed = time.monotonic() - started
    step_result = StepResult(
        name=name,
        argv=tuple(argv),
        returncode=result.returncode,
        duration_seconds=elapsed,
    )
    if step_sink is not None:
        step_sink(step_result, result)
    if not quiet and echo_stdout and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    elif not quiet and result.stdout and echo_stdout_prefixes:
        selected = [
            line
            for line in result.stdout.splitlines()
            if any(line.startswith(prefix) for prefix in echo_stdout_prefixes)
        ]
        if selected:
            print("\n".join(selected), flush=True)
    if not quiet:
        print(f"step {name}: exit {result.returncode} ({elapsed:.1f}s)", flush=True)
    if result.returncode not in allowed_exit_codes:
        raise BatchStepError(
            f"step {name} failed with exit {result.returncode}\n"
            f"stderr (last {_STDERR_SUMMARY_LINES} lines):\n{_stderr_summary(result.stderr)}",
            stage=name,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    if not quiet and result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def _finite_number(value: object) -> float | None:
    """A JSON number that can be rendered, or None. ``bool`` is not a number here."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _delta_ticker_labels(rows: object) -> list[str]:
    """Keep the view's ticker order; the final notification renderer limits display.

    Malformed entries degrade independently, without losing identifiable tickers.
    """

    if not isinstance(rows, list):
        return []
    labels: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        parts = [ticker.strip()]
        name = row.get("company_name")
        if isinstance(name, str) and name.strip():
            parts.append(sanitize_one_line(name, _DELTA_TICKER_NAME_MAX_CHARS))
        er = _finite_number(row.get("er_annual_pct"))
        if er is not None:
            parts.append(f"E[r]{er:+.1f}%")
        label = sanitize_one_line(" ".join(parts), _DELTA_TICKER_LABEL_MAX_CHARS)
        if not label:
            continue
        labels.append(label)
    return labels


def _read_daily_delta(path: Path, notice: _Notice) -> None:
    """Carry the day's candidate entries and exits into the notice.

    The notification is the only channel that reaches a reader without being
    opened, so the names that moved belong in it. ``delta_measured`` separates a
    day with no movement from a view that could not be read: an empty list alone
    would say the same thing for both. The view is another process's output, so
    nothing about its shape may cost the run its notification.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    entered = payload.get("entered")
    exited = payload.get("exited")
    if not isinstance(entered, list) or not isinstance(exited, list):
        return
    unavailable = payload.get("unavailable")
    if not isinstance(unavailable, list):
        return
    for section in ("screening_run", "previous_screening_run", "candidates"):
        if section in unavailable:
            notice.delta_unmeasured_reason = section
            return
    if payload.get("method_changed") is True:
        notice.delta_unmeasured_reason = "手法変更"
        return
    notice.delta_measured = True
    notice.delta_unmeasured_reason = ""
    notice.entered = _delta_ticker_labels(entered)
    notice.exited = _delta_ticker_labels(exited)


def _parse_edinet_quarantine_metrics(stdout: str) -> tuple[int, int, str]:
    """Read the fail-closed event/ticker counts from a successful extraction."""

    for line in reversed(stdout.splitlines()):
        if not line.startswith("EDINET extraction summary: "):
            continue
        match = _EDINET_QUARANTINE_RE.search(line)
        if match is not None:
            return (
                int(match.group("events")),
                int(match.group("tickers")),
                match.group("sample"),
            )
        break
    raise BatchStepError(
        "extract-edinet-metrics succeeded without quarantine counters",
        stage="extract-edinet-metrics",
    )


def _stderr_summary(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    if not lines:
        return "  (empty)"
    return "\n".join(f"  {line}" for line in lines[-_STDERR_SUMMARY_LINES:])


@dataclass(frozen=True, slots=True)
class _RunView:
    run_revision_id: str
    universe_size: int
    analyzed_count: int
    candidate_count: int


def _read_run_view(run_yaml: Path) -> _RunView:
    """Read identity/counts from the committed run export; invalid is not empty."""
    try:
        payload = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("stock analysis run export must be an object")
        revision = payload.get("run_revision_id")
        if not isinstance(revision, str) or not revision:
            raise ValueError("run_revision_id must be a non-empty string")
        universe = payload.get("universe_size")
        if type(universe) is not int or universe < 0:
            raise ValueError("universe_size must be a non-negative integer")
        analyses = payload.get("stock_analyses")
        if not isinstance(analyses, list) or any(not isinstance(row, dict) for row in analyses):
            raise ValueError("stock_analyses must be an array of objects")
        validate_candidate_run(payload, analyses)
        count = payload["candidate_discovery"]["diagnostics"]["candidate_count"]
        return _RunView(revision, universe, len(analyses), count)
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise BatchStepError(
            f"screening run YAML view is invalid or unreadable: {error}",
            stage="screening-run",
        ) from error


@dataclass(frozen=True, slots=True)
class _IndicatorSeries:
    series_id: str
    frequency: str
    provider: str
    kind: str


def _parse_indicator_series(stdout: str) -> list[_IndicatorSeries]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BatchStepError(
            f"market-context indicators list --format json output is not parseable JSON: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise BatchStepError("market-context indicators list --format json output is not a list")
    series: list[_IndicatorSeries] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise BatchStepError(
                "market-context indicators list --format json row is not an object"
            )
        try:
            series.append(
                _IndicatorSeries(
                    series_id=str(entry["series_id"]),
                    frequency=str(entry["frequency"]),
                    provider=str(entry["provider"]),
                    kind=str(entry["kind"]),
                )
            )
        except KeyError as exc:
            raise BatchStepError(
                f"market-context indicators list --format json row missing field {exc}"
            ) from exc
    return series


def _indicator_refresh_groups(series: Sequence[_IndicatorSeries]) -> list[tuple[int, list[str]]]:
    """Group series by frequency window, refreshing base (http) before derived (local).

    A derived series reads other series from the store, so every ``http`` series
    is refreshed before any ``local`` one; within each kind, series are grouped by
    their frequency refresh window.
    """

    groups: dict[tuple[int, int], list[str]] = {}
    for item in series:
        window = LATEST_FETCH_LOOKBACK_DAYS.get(
            item.frequency,
            DEFAULT_LATEST_LOOKBACK_DAYS,
        )
        kind_order = 1 if item.kind == "local" else 0
        groups.setdefault((kind_order, window), []).append(item.series_id)
    return [(window, series_ids) for (_kind_order, window), series_ids in sorted(groups.items())]


def _run_screening_run(
    runner: CommandRunner,
    *,
    root: Path,
    asof_arg: str,
    quiet: bool = False,
    step_sink: StepSink | None = None,
) -> _RunView:
    """Run screening and read the run view (revision id + counts) it writes.

    The run publishes to the store either way; ``--output-path`` mirrors that
    publication to a throwaway file whose ``run_revision_id`` key is the stable
    contract. Exit 2 is a
    published run with partial-quality warnings, so the chain continues.
    """

    with tempfile.TemporaryDirectory() as tmp:
        run_yaml = Path(tmp) / "screening-run.yaml"
        argv = [
            _ENGINE,
            "screening",
            "run",
            "--asof",
            asof_arg,
            "--output-path",
            str(run_yaml),
        ]
        result = _run_step(
            runner,
            name="screening-run",
            argv=argv,
            cwd=root,
            allowed_exit_codes=(0, 2),
            quiet=quiet,
            step_sink=step_sink,
        )
        if result.returncode == 2 and not quiet:
            print(
                "note: screening run finished with partial warning "
                "(run is published; reasons above)",
                flush=True,
            )
        return _read_run_view(run_yaml)


@dataclass(slots=True)
class _Notice:
    """What the batch leaves for the Discord notifier on every terminal path.

    The as-of it ran for, whether the business-day gate skipped it, the first
    fatal or deferred failure stage, and the names that entered or left the
    candidate list. The exit code carries the outcome itself.
    """

    asof: str = ""
    skipped: bool = False
    screening_status: str = "pending"
    market_updated: bool = False
    failed_stage: str | None = None
    delta_measured: bool = False
    delta_unmeasured_reason: str = "view_unreadable"
    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "asof": self.asof,
            "skipped": self.skipped,
            "screening_status": self.screening_status,
            "market_updated": self.market_updated,
            "failed_stage": self.failed_stage,
            "delta_measured": self.delta_measured,
            "delta_unmeasured_reason": self.delta_unmeasured_reason,
            "entered": list(self.entered),
            "exited": list(self.exited),
        }


def _write_notice(path: Path | None, notice: _Notice) -> None:
    # Writing the notice must not undo a completed publish or replace a real
    # failure: the data work is already done, and the exit code is what the
    # workflow reads. A notice that cannot be written costs only the delta lines.
    if path is None:
        return
    try:
        write_json_atomic(path, notice.to_json())
    except OSError as exc:
        print(f"error: batch notice could not be written: {exc}", file=sys.stderr)


def run_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
    scheduled: bool = False,
    notice_output: Path | None = None,
) -> int:
    return run_daily_batch_structured(
        root=root,
        output_dir=output_dir,
        asof=asof,
        runner=runner,
        scheduled=scheduled,
        notice_output=notice_output,
    ).exit_code


def run_daily_batch_structured(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
    scheduled: bool = False,
    notice_output: Path | None = None,
    quiet: bool = False,
    step_sink: StepSink | None = None,
    gate_explicit_asof: bool = False,
) -> DailyBatchResult:
    notice = _Notice()
    steps: list[StepResult] = []

    def record(step: StepResult, command: CommandResult) -> None:
        steps.append(step)
        if step_sink is not None:
            step_sink(step, command)

    # Observe commits without opening a read transaction, copying the store or
    # inventing a source recovery ledger. This reader never writes to the store.
    market_path = (root / _MARKET_DB_RELPATH).resolve()
    connection_context = (
        closing(sqlite3.connect(f"{market_path.as_uri()}?mode=ro", uri=True, isolation_level=None))
        if market_path.is_file()
        else nullcontext(None)
    )
    with connection_context as connection:
        version = None
        if connection is not None:
            # The gate/export report an unreadable store; this diagnostic
            # must not prevent the independent indicator refresh.
            with suppress(sqlite3.Error):
                version = connection.execute("PRAGMA data_version").fetchone()[0]
        try:
            result = _execute_daily_batch(
                root=root,
                output_dir=output_dir,
                asof=asof,
                scheduled=scheduled,
                runner=runner,
                notice=notice,
                quiet=quiet,
                step_sink=record,
                gate_explicit_asof=gate_explicit_asof,
            )
        except (BatchStepError, CalendarCoverageError) as exc:
            notice.failed_stage = exc.stage or "batch"
            _write_notice(notice_output, notice)
            raise
        if connection is not None and version is not None:
            notice.market_updated = (
                connection.execute("PRAGMA data_version").fetchone()[0] != version
            )
    _write_notice(notice_output, notice)
    return DailyBatchResult(
        status=result.status,
        asof=result.asof,
        exit_code=result.exit_code,
        run_revision_id=result.run_revision_id,
        candidate_count=result.candidate_count,
        deferred_failure_count=result.deferred_failure_count,
        steps=tuple(steps),
        screening_status=notice.screening_status,
        market_updated=notice.market_updated,
    )


def _execute_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    scheduled: bool,
    runner: CommandRunner,
    notice: _Notice,
    quiet: bool = False,
    step_sink: StepSink | None = None,
    gate_explicit_asof: bool = False,
) -> DailyBatchResult:
    target = asof or (
        batch_target_date(datetime.now(UTC)) if scheduled else datetime.now(_JST).date()
    )
    asof_arg = target.isoformat()
    notice.asof = asof_arg
    deferred_failures: list[str] = []

    def _record_deferred(exc: BatchStepError | CalendarCoverageError) -> None:
        if not quiet:
            print(f"deferred failure: {exc}", file=sys.stderr, flush=True)
        if notice.failed_stage is None:
            notice.failed_stage = exc.stage or "batch"
        deferred_failures.append(str(exc))

    screening_enabled = True
    if asof is None or gate_explicit_asof:
        try:
            screening_enabled = _require_business_day(root / _MARKET_DB_RELPATH, target)
            if not screening_enabled:
                notice.screening_status = "skipped_non_business_day"
                if not quiet:
                    print(f"skip Screening: {asof_arg} は非営業日; indicators continue", flush=True)
        except CalendarCoverageError as exc:
            screening_enabled = False
            notice.screening_status = "failed"
            _record_deferred(exc)
    if not quiet:
        print(f"daily batch start: asof={asof_arg}", flush=True)

    run_view: _RunView | None = None
    if screening_enabled:
        # Keep the acquisition groups independent. Their existing writers own
        # validation and chunk transactions; a later failure cannot undo a commit.
        for name, command in (
            ("refresh-edinet-documents", "refresh-edinet-documents"),
            ("bootstrap-cache", "bootstrap-cache"),
            ("extract-edinet-metrics", "extract-edinet-metrics"),
            ("verify-cache-coverage", "verify-cache-coverage"),
        ):
            try:
                command_result = _run_step(
                    runner,
                    name=name,
                    argv=(_ENGINE, "screening", command, "--asof", asof_arg),
                    cwd=root,
                    quiet=quiet,
                    step_sink=step_sink,
                )
                if command == "extract-edinet-metrics":
                    _parse_edinet_quarantine_metrics(command_result.stdout)
            except BatchStepError as exc:
                _record_deferred(exc)
        if not deferred_failures:
            try:
                run_view = _run_screening_run(
                    runner, root=root, asof_arg=asof_arg, quiet=quiet, step_sink=step_sink
                )
            except BatchStepError as exc:
                _record_deferred(exc)
        notice.screening_status = "published" if run_view is not None else "failed"

    try:
        _run_step(
            runner,
            name="edinet-research-facts",
            argv=(_ENGINE, "screening", "extract-edinet-facts", "--asof", asof_arg),
            cwd=root,
            quiet=quiet,
            step_sink=step_sink,
        )
    except BatchStepError as exc:
        _record_deferred(exc)

    refresh_groups: list[tuple[int, list[str]]] = []
    try:
        indicator_list = _run_step(
            runner,
            name="indicator-list",
            argv=(_ENGINE, "market-context", "indicators", "list", "--format", "json"),
            cwd=root,
            echo_stdout=False,
            quiet=quiet,
            step_sink=step_sink,
        )
        refresh_groups = _indicator_refresh_groups(_parse_indicator_series(indicator_list.stdout))
    except BatchStepError as exc:
        _record_deferred(exc)
    for window_days, series_ids in refresh_groups:
        start = target - timedelta(days=window_days)
        try:
            _run_step(
                runner,
                name=f"indicator-refresh-{window_days}d",
                argv=(
                    _ENGINE,
                    "market-context",
                    "indicators",
                    "refresh",
                    *series_ids,
                    "--start",
                    start.isoformat(),
                    "--end",
                    asof_arg,
                ),
                cwd=root,
                echo_stdout=False,
                echo_stdout_prefixes=("registry-prune\t",),
                quiet=quiet,
                step_sink=step_sink,
            )
        except BatchStepError as exc:
            _record_deferred(exc)

    export_argv = [
        sys.executable,
        "-m",
        _EXPORT_MODULE,
        "--output-dir",
        str(output_dir),
        "--batch",
        "daily",
        "--repo-root",
        str(root),
    ]
    if run_view is None:
        export_argv.append("--skip-history")
    _run_step(
        runner,
        name="export-read-models",
        argv=export_argv,
        cwd=root,
        quiet=quiet,
        step_sink=step_sink,
    )
    if run_view is not None:
        _read_daily_delta(output_dir / "views" / "daily-delta.json", notice)
        try:
            _run_step(
                runner,
                name="screening-prune",
                argv=(_ENGINE, "screening", "prune"),
                cwd=root,
                quiet=quiet,
                step_sink=step_sink,
            )
        except BatchStepError as exc:
            _record_deferred(exc)
    else:
        notice.delta_unmeasured_reason = notice.screening_status

    # The exchange publishes its schedule only weeks ahead, so a follow-up task
    # created a quarter out carries an estimate until the real date enters that
    # window. Comparing daily is what surfaces the day it becomes knowable. It
    # reads nothing the publish produced and writes nothing, so it runs after the
    # publish and a failure degrades rather than blocking it.
    try:
        _run_step(
            runner,
            name="task-reconcile-earnings",
            argv=(_ENGINE, "task", "reconcile-earnings"),
            cwd=root,
            echo_stdout=False,
            # The report is the step's only product, so the findings have to reach
            # the log. Confirmations are the normal case and would be ~100 lines a
            # day of noise, so only the rows that need a human are echoed.
            echo_stdout_prefixes=("reconcile-earnings\t",),
            quiet=quiet,
            step_sink=step_sink,
        )
    except BatchStepError as exc:
        _record_deferred(exc)

    if not quiet:
        print(
            "daily batch done: "
            f"asof={asof_arg}; screening={notice.screening_status}; "
            f"run_revision_id={None if run_view is None else run_view.run_revision_id}; "
            f"candidate_count={None if run_view is None else run_view.candidate_count}",
            flush=True,
        )
    if deferred_failures:
        print(
            f"error: export published, but {len(deferred_failures)} deferred step(s) failed:",
            file=sys.stderr,
        )
        for failure in deferred_failures:
            print(f"- {failure}", file=sys.stderr)
        return DailyBatchResult(
            "deferred",
            asof_arg,
            _EXIT_DEFERRED_FAILURE,
            None if run_view is None else run_view.run_revision_id,
            None if run_view is None else run_view.candidate_count,
            len(deferred_failures),
        )
    return DailyBatchResult(
        "machine_complete",
        asof_arg,
        0,
        None if run_view is None else run_view.run_revision_id,
        None if run_view is None else run_view.candidate_count,
    )


def _root_error(root: Path) -> str | None:
    return repository_root_error(root, label="--repo-root")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_batch",
        description=(
            "run independent Screening and indicator machine updates -> read-model export"
        ),
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--asof",
        type=str,
        help=(
            "run for this date (YYYY-MM-DD) and skip the business-day gate "
            "(manual rerun / past date); default is today in JST, gated by the market calendar"
        ),
    )
    target_group.add_argument(
        "--scheduled",
        action="store_true",
        help=(
            "resolve the target from the 07:43 UTC batch cron and apply the business-day gate; "
            "for the scheduled workflow only"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="serving read-model output directory passed to export_read_models",
    )
    parser.add_argument(
        "--notice-output",
        type=Path,
        default=None,
        help="write the JSON notice the Discord notifier reads to this path on every terminal path",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        help="write the structured daily result as JSON without changing the existing chain",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="state root used for the default daily-manifest.json path",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress step output; use the manifest/log files for detail",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    error = _root_error(root)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    notice_output = args.notice_output.resolve() if args.notice_output is not None else None
    asof: date | None = None
    if args.asof is not None:
        try:
            asof = date.fromisoformat(args.asof)
        except ValueError:
            print(f"error: asof '{args.asof}' is not a valid YYYY-MM-DD date", file=sys.stderr)
            return 1
    try:
        result = run_daily_batch_structured(
            root=root,
            output_dir=args.output_dir.resolve(),
            asof=asof,
            runner=_run_subprocess,
            scheduled=args.scheduled,
            notice_output=notice_output,
            quiet=args.quiet or args.format == "json",
        )
        manifest_out = args.manifest_out
        if manifest_out is None and args.state_dir is not None:
            manifest_out = args.state_dir / "daily-manifest.json"
        if manifest_out is not None:
            try:
                write_json_atomic(manifest_out.resolve(), result.to_json())
            except OSError as exc:
                print(
                    f"observability_degraded: daily manifest could not be written: {exc}",
                    file=sys.stderr,
                )
        if args.format == "json":
            print(json.dumps(result.to_json(), ensure_ascii=False, separators=(",", ":")))
        return result.exit_code
    except (BatchStepError, CalendarCoverageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
