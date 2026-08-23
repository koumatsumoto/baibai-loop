"""Run the local daily machine batch as one command.

Order: business-day gate -> screening cache coverage (bootstrap on demand) ->
``screening run`` -> ``screening select`` -> macro series refresh ->
read-model export -> run-store prune. Every heavy step goes
through the public ``baibai-engine`` CLI (or the Web materializer module) as a subprocess,
so the stable CLI contract carries the business logic. The only in-process reads
are the two ``baibai_engine.read_api`` query-only helpers this orchestrator needs
before it can build a CLI command: the business-day gate and the previous-run
resolution for ``select``. Each step prints its command line and an
``exit <code> (<seconds>s)`` line to stdout so a scheduled workflow log is
readable as-is.

Failure policy: the screening chain is fatal (without a publishable run there
is nothing new to export), while macro series refresh failures are deferred —
the export still publishes the fresh screening result and the batch exits with
code 3 afterwards so a scheduled workflow still reports the failure while the
publish stands. ``screening run`` exit 2 is a published run with partial-quality
warnings and the chain continues.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import sqlite3
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_batch.observability.summary import (
    BATCH_STATUS_DEGRADED,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_OK,
    BATCH_SUMMARY_SCHEMA_VERSION,
    ERROR_IMPACT_DEGRADED,
    ERROR_IMPACT_FAILED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_SUCCEEDED,
    BatchError,
    BatchExecutionSummary,
    BatchResult,
    SummaryValidationError,
    sanitize_one_line,
    write_json_atomic,
)
from baibai_engine.batch_api import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    parse_refresh_failure_count,
    repository_root_error,
)
from baibai_engine.read_api import market_calendar_business_day, previous_run_revision_id

_JST = ZoneInfo("Asia/Tokyo")
_ENGINE = "baibai-engine"
_MARKET_DB_RELPATH = MARKET_DB_PATH
_RUNS_DB_RELPATH = RUNS_DB_PATH
_EXPORT_MODULE = "baibai_web.materialize"
_STDERR_SUMMARY_LINES = 20
# `verify-cache-coverage` prints this marker on stdout for a genuine cache gap;
# an exit 1 without it (broken rules, unreadable store) is a crash, not a gap.
_COVERAGE_INCOMPLETE_MARKER = "SQLite cache coverage incomplete"
# Exit 3 means the screening result was published and exported, but a deferred
# (macro / prune) step failed afterwards.
_EXIT_DEFERRED_FAILURE = 3
# The Research Gate input population size the opportunity path uses.
_SELECT_LONGLIST_TOP = 20
_EDINET_QUARANTINE_RE = re.compile(
    r"\bquarantined_events=(?P<events>\d+)\s+"
    r"quarantined_tickers=(?P<tickers>\d+)\s+"
    r"quarantine_sample=(?P<sample>[^\s;]+)"
)
# How many names the notification carries per delta side. The reader acts on the top
# of the list on the evening of a drop; the full set stays in the delta view, and
# ``delta_entered`` / ``delta_exited`` keep carrying the counts so a capped list
# never hides its own remainder.
_DELTA_TICKERS_NAMED = 5
# Bound one rendered entry so a long company name cannot crowd out the rest of the
# notification. The name is bounded first so the estimate, which is what ranks the
# entry, survives the truncation.
_DELTA_TICKER_NAME_MAX_CHARS = 24
_DELTA_TICKER_LABEL_MAX_CHARS = 48


class BatchStepError(RuntimeError):
    """A batch step failed or produced output the chain cannot continue from.

    The free-text message stays human-facing (stderr / GitHub Actions log). The
    optional structured fields let the summary builder derive a redacted typed
    error without ever feeding the message text into the notification payload.

    ``stderr`` carries the step's unredacted output for a handler that has to read
    what the step reported. It is deliberately not one of the ``scalars``, which
    cross into the notification contract: source output can name credentials and
    paths, and nothing about it is validated for publication.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        returncode: int | None = None,
        error_code: str | None = None,
        stderr: str = "",
        **scalars: object,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.returncode = returncode
        self.error_code = error_code
        self.stderr = stderr
        self.scalars = scalars


class CalendarCoverageError(RuntimeError):
    """The market calendar cannot answer the business-day question for a date."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        stage: str = "calendar",
        **scalars: object,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.scalars = scalars


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


type CommandRunner = Callable[[Sequence[str], Path], CommandResult]


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
    each stop the batch with a distinct message instead of silently continuing.
    """

    if not market_db.is_file():
        raise CalendarCoverageError(
            f"market SQLite does not exist: {market_db}", error_code="calendar_store_missing"
        )
    try:
        result = market_calendar_business_day(market_db, day)
    except sqlite3.Error as exc:
        raise CalendarCoverageError(
            f"market calendar is unreadable in {market_db}: {exc}", error_code="calendar_unreadable"
        ) from exc
    if result is None:
        raise CalendarCoverageError(
            f"market calendar does not cover {day.isoformat()}; "
            "refresh the calendar cache before running the daily batch",
            error_code="calendar_uncovered",
            asof=day.isoformat(),
        )
    return result


def _normalize_stage(name: str) -> str:
    """Map a display step name to the allowlisted typed-error stage.

    Display names carry suffixes the typed-error vocabulary does not (the
    ``macro-refresh-14d`` window, the ``verify-cache-coverage(recheck)`` retry);
    collapse them to their stable stage so redacted errors stay allowlisted.
    """

    base = name.split("(", 1)[0]
    if base.startswith("macro-refresh"):
        return "macro-refresh"
    return base


def _typed_error(
    exc: BatchStepError | CalendarCoverageError, impact: str = ERROR_IMPACT_FAILED
) -> BatchError:
    """Derive a redacted typed error from a structured batch exception.

    Only the allowlisted code / stage / return code / validated scalars cross
    into the notification contract; the human-facing exception message never does.
    ``impact`` is "degraded" for deferred failures (the run still publishes) and
    "failed" for fatal ones.
    """

    if isinstance(exc, CalendarCoverageError):
        return BatchError.build(code=exc.error_code, stage=exc.stage, impact=impact, **exc.scalars)
    if exc.error_code is not None:
        return BatchError.build(
            code=exc.error_code,
            stage=exc.stage or "batch",
            impact=impact,
            **exc.scalars,
        )
    if exc.stage is not None and exc.returncode is not None:
        return BatchError.subprocess_failure(
            stage=exc.stage, returncode=exc.returncode, impact=impact
        )
    return BatchError.build(code="batch_failed", stage=exc.stage or "batch", impact=impact)


def _run_step(
    runner: CommandRunner,
    *,
    name: str,
    argv: Sequence[str],
    cwd: Path,
    allowed_exit_codes: Sequence[int] = (0,),
    echo_stdout: bool = True,
    echo_stdout_prefixes: Sequence[str] = (),
) -> CommandResult:
    print(f"$ {' '.join(argv)}", flush=True)
    started = time.monotonic()
    try:
        result = runner(argv, cwd)
    except FileNotFoundError as exc:
        raise BatchStepError(
            f"step {name}: command not found: {argv[0]}",
            stage=_normalize_stage(name),
            error_code="command_not_found",
        ) from exc
    elapsed = time.monotonic() - started
    if echo_stdout and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    elif result.stdout and echo_stdout_prefixes:
        selected = [
            line
            for line in result.stdout.splitlines()
            if any(line.startswith(prefix) for prefix in echo_stdout_prefixes)
        ]
        if selected:
            print("\n".join(selected), flush=True)
    print(f"step {name}: exit {result.returncode} ({elapsed:.1f}s)", flush=True)
    if result.returncode not in allowed_exit_codes:
        raise BatchStepError(
            f"step {name} failed with exit {result.returncode}\n"
            f"stderr (last {_STDERR_SUMMARY_LINES} lines):\n{_stderr_summary(result.stderr)}",
            stage=_normalize_stage(name),
            returncode=result.returncode,
            stderr=result.stderr,
        )
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def _failed_series_count(exc: BatchStepError, *, requested: int) -> int:
    """How many series of a refresh group actually failed.

    One failing source must not be counted as the whole group failing: the group
    is a batching decision, and reporting its size as the failure count inflates
    what the run summary and its notification show. The refresh reports its own
    count, so read that and only fall back to the group size when the step failed
    before reporting one — over-counting is the safe direction for a health signal.

    Nothing about reading a count is worth failing a run over. This runs inside
    the handler that keeps a macro failure from blocking the publish, and an
    exception escaping here would leave the day's screening result unexported,
    so any trouble reading resolves to the same conservative fallback.
    """

    try:
        reported = parse_refresh_failure_count(exc.stderr)
    except Exception:
        return requested
    if reported is None or reported > requested:
        return requested
    return reported


def _finite_number(value: object) -> float | None:
    """A JSON number that can be rendered, or None. ``bool`` is not a number here."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _delta_ticker_labels(rows: object) -> list[str]:
    """Name the tickers on one side of the pool delta, best estimate first.

    A day that moves names into or out of the pool is worth acting on that evening,
    and a count alone does not say which names to look at. The list is capped
    because the reader acts on the top of it; ``delta_entered`` and ``delta_exited``
    keep carrying the full counts, so a capped list never hides its own remainder.

    Each field degrades on its own: a row missing a company name or an estimate
    still names its ticker, since a partly-known entry is still the pointer the
    reader needs. A row that cannot even be identified by ticker is dropped rather
    than reported blank. The view is written by a separate process, so nothing about
    its shape may cost the run its notification — every value is read defensively
    and rendered through the same one-line sanitizer the notification contract uses.
    """

    if not isinstance(rows, list):
        return []
    ranked: list[tuple[int, float, str, str]] = []
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
        # Estimate descending, rows without an estimate last, ticker as the
        # tie-break so the same pool always renders the same way.
        ranked.append((0 if er is not None else 1, -(er or 0.0), ticker, label))
    ranked.sort()
    return [label for *_, label in ranked[:_DELTA_TICKERS_NAMED]]


def _daily_delta_metrics(path: Path) -> dict[str, object]:
    """Read the exported delta counts so the run notification carries them.

    The notification is the only channel that reaches a reader without being
    opened, so the day's change counts belong in it. Every key is reported on every
    run because the summary schema requires it, and ``delta_measured`` separates a
    day with no changes from a view that could not be read — zero counts alone
    would say the same thing for both. ``delta_entered_tickers`` and
    ``delta_exited_tickers`` name the tickers behind the two counts; each is empty
    whenever there is nothing to name, which the schema requires to be a
    present-but-empty list.
    """

    absent: dict[str, object] = {
        "delta_measured": False,
        "delta_entered": 0,
        "delta_entered_tickers": [],
        "delta_exited": 0,
        "delta_exited_tickers": [],
        "delta_er_moves": 0,
        "delta_holdings": 0,
        "delta_macro_flags": 0,
        "delta_macro_extremes": 0,
        "delta_unavailable": "view_unreadable",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return absent
    if not isinstance(payload, dict):
        return absent
    counts: dict[str, object] = {"delta_measured": True}
    for name in ("entered", "exited", "er_moves", "holdings", "macro_flags", "macro_extremes"):
        value = payload.get(name)
        if not isinstance(value, list):
            return absent
        counts[f"delta_{name}"] = len(value)
    counts["delta_entered_tickers"] = _delta_ticker_labels(payload.get("entered"))
    counts["delta_exited_tickers"] = _delta_ticker_labels(payload.get("exited"))
    unavailable = payload.get("unavailable")
    counts["delta_unavailable"] = (
        ",".join(str(item) for item in unavailable) if isinstance(unavailable, list) else ""
    )
    return counts


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
        error_code="edinet_summary_invalid",
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
    candidate_count: int


def _read_run_view(run_yaml: Path) -> _RunView:
    """Read run_revision_id + universe/candidate counts from the run YAML view.

    ``screening run --output-path`` writes ``universe_size`` and ``candidates``
    alongside ``run_revision_id``; the counts default to 0 when a view omits them
    so a minimal view still surfaces the stable ``run_revision_id`` contract.
    """

    if not run_yaml.is_file():
        raise BatchStepError(
            "screening run did not write the --output-path YAML view",
            stage="screening-run",
            error_code="run_view_invalid",
        )
    try:
        payload = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BatchStepError(
            f"screening run YAML view is unreadable: {exc}",
            stage="screening-run",
            error_code="run_view_invalid",
        ) from exc
    if isinstance(payload, dict):
        run_revision_id = payload.get("run_revision_id")
        if isinstance(run_revision_id, str) and run_revision_id:
            universe = payload.get("universe_size")
            candidates = payload.get("candidates")
            universe_size = (
                universe if isinstance(universe, int) and not isinstance(universe, bool) else 0
            )
            candidate_count = len(candidates) if isinstance(candidates, list) else 0
            return _RunView(run_revision_id, universe_size, candidate_count)
    raise BatchStepError(
        "screening run YAML view does not contain run_revision_id",
        stage="screening-run",
        error_code="run_view_invalid",
    )


@dataclass(frozen=True, slots=True)
class _SelectionView:
    selection_id: str
    selected_count: int


def _parse_selection_view(stdout: str) -> _SelectionView:
    """Read selection_id + selected count from the ``screening select`` YAML output."""

    try:
        payload = yaml.safe_load(stdout)
    except yaml.YAMLError as exc:
        raise BatchStepError(
            f"screening select output is not parseable YAML: {exc}",
            stage="screening-select",
            error_code="select_output_invalid",
        ) from exc
    if isinstance(payload, dict):
        selection_id = payload.get("selection_id")
        if isinstance(selection_id, str) and selection_id:
            recommendations = payload.get("recommendations")
            selected_count = len(recommendations) if isinstance(recommendations, list) else 0
            return _SelectionView(selection_id, selected_count)
    raise BatchStepError(
        "screening select output does not contain selection_id",
        stage="screening-select",
        error_code="select_output_invalid",
    )


@dataclass(frozen=True, slots=True)
class _MacroSeries:
    series_id: str
    frequency: str
    provider: str
    kind: str


def _parse_macro_series(stdout: str) -> list[_MacroSeries]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BatchStepError(
            f"macro list --format json output is not parseable JSON: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise BatchStepError("macro list --format json output is not a list")
    series: list[_MacroSeries] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise BatchStepError("macro list --format json row is not an object")
        try:
            series.append(
                _MacroSeries(
                    series_id=str(entry["series_id"]),
                    frequency=str(entry["frequency"]),
                    provider=str(entry["provider"]),
                    kind=str(entry["kind"]),
                )
            )
        except KeyError as exc:
            raise BatchStepError(f"macro list --format json row missing field {exc}") from exc
    return series


def _macro_refresh_groups(series: Sequence[_MacroSeries]) -> list[tuple[int, list[str]]]:
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


def _run_screening_run(runner: CommandRunner, *, root: Path, asof_arg: str) -> _RunView:
    """Run screening and read the run view (revision id + counts) it writes.

    The run publishes to the store either way; ``--output-path`` mirrors that
    publication to a throwaway file whose ``run_revision_id`` key is the stable
    contract (the same field ``select`` echoes as ``selection_id``). Exit 2 is a
    published run with partial-quality warnings, so the chain continues.
    """

    with tempfile.TemporaryDirectory() as tmp:
        run_yaml = Path(tmp) / "screening-run.yaml"
        result = _run_step(
            runner,
            name="screening-run",
            argv=(
                _ENGINE,
                "screening",
                "run",
                "--asof",
                asof_arg,
                "--output-path",
                str(run_yaml),
            ),
            cwd=root,
            allowed_exit_codes=(0, 2),
        )
        if result.returncode == 2:
            print(
                "note: screening run finished with partial warning "
                "(run is published; reasons above)",
                flush=True,
            )
        return _read_run_view(run_yaml)


_BATCH_DATASETS: dict[str, tuple[str, ...]] = {
    "screening": ("screening-run", "screening-selection"),
    "macro": ("macro-series",),
    "serving-export": ("views", "history"),
    "prune": ("runs-store",),
    "task-reconcile": ("follow-up-tasks",),
}


@dataclass(slots=True)
class _BatchRecorder:
    """Accumulates logical batch results so a summary exists on every terminal path.

    ``section`` / ``section_mono`` mark the logical batch currently running so a
    fatal failure can be attributed to a failed batch result with its own duration
    even when the failure aborts the section before its result is built.
    """

    asof: str = ""
    skipped: bool = False
    local_export: bool = False
    section: str = "screening"
    section_mono: float = field(default_factory=time.monotonic)
    batches: list[BatchResult] = field(default_factory=list)

    def enter_section(self, name: str) -> float:
        self.section = name
        self.section_mono = time.monotonic()
        return self.section_mono


def _finalize_summary(
    summary_output: Path | None,
    *,
    recorder: _BatchRecorder,
    outcome: str,
    started_at: datetime,
    started_mono: float,
) -> None:
    """Validate and atomically write the batch execution summary.

    The round-trip through ``from_json`` validates the composed model; a summary
    that fails validation raises instead of writing, so a broken summary never
    lets the run report success.
    """

    if summary_output is None:
        return
    summary = BatchExecutionSummary(
        schema_version=BATCH_SUMMARY_SCHEMA_VERSION,
        asof=recorder.asof,
        outcome=outcome,
        started_at=started_at.isoformat(),
        finished_at=datetime.now(_JST).isoformat(),
        duration_seconds=time.monotonic() - started_mono,
        batches=tuple(recorder.batches),
        local_export=recorder.local_export,
    )
    validated = BatchExecutionSummary.from_json(summary.to_json())
    write_json_atomic(summary_output, validated.to_json())


def _finalize_failed_summary(
    summary_output: Path | None,
    *,
    recorder: _BatchRecorder,
    exc: BatchStepError | CalendarCoverageError,
    started_at: datetime,
    started_mono: float,
) -> None:
    if summary_output is None:
        return
    # A fatal batch failure must stay fatal even if the summary cannot be composed;
    # the original exception is what the caller reports. Classifying the error is
    # part of composing the summary — a stage the schema does not know raises there,
    # so it has to sit inside the guard or an unregistered stage would replace the
    # real failure with a validation error and lose the summary entirely.
    with contextlib.suppress(SummaryValidationError):
        recorder.batches.append(
            BatchResult(
                batch_name=recorder.section,
                datasets=_BATCH_DATASETS[recorder.section],
                status=BATCH_STATUS_FAILED,
                duration_seconds=time.monotonic() - recorder.section_mono,
                metrics={},
                errors=(_typed_error(exc),),
            )
        )
        _finalize_summary(
            summary_output,
            recorder=recorder,
            outcome=OUTCOME_FAILED,
            started_at=started_at,
            started_mono=started_mono,
        )


def run_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
    summary_output: Path | None = None,
) -> int:
    started_mono = time.monotonic()
    started_at = datetime.now(_JST)
    recorder = _BatchRecorder()
    try:
        exit_code = _execute_daily_batch(
            root=root, output_dir=output_dir, asof=asof, runner=runner, recorder=recorder
        )
    except (BatchStepError, CalendarCoverageError) as exc:
        _finalize_failed_summary(
            summary_output,
            recorder=recorder,
            exc=exc,
            started_at=started_at,
            started_mono=started_mono,
        )
        raise
    if recorder.skipped:
        outcome = OUTCOME_SKIPPED
    elif exit_code == _EXIT_DEFERRED_FAILURE:
        outcome = OUTCOME_DEGRADED
    else:
        outcome = OUTCOME_SUCCEEDED
    # Writing the observability summary must not undo a completed publish: the
    # data work is already done and the export already wrote views/. A schema
    # drift here would otherwise exit non-zero, which skips both upload steps and
    # discards the day's screening result. The notifier reports the missing
    # summary as [FAILED], so the failure stays visible.
    try:
        _finalize_summary(
            summary_output,
            recorder=recorder,
            outcome=outcome,
            started_at=started_at,
            started_mono=started_mono,
        )
    except SummaryValidationError as exc:
        print(f"error: batch summary could not be written: {exc}", file=sys.stderr)
    return exit_code


def _execute_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
    recorder: _BatchRecorder,
) -> int:
    screening_mono = recorder.enter_section("screening")
    if asof is None:
        target = datetime.now(_JST).date()
        recorder.asof = target.isoformat()
        if not _require_business_day(root / _MARKET_DB_RELPATH, target):
            print(f"skip: {target.isoformat()} は非営業日", flush=True)
            recorder.skipped = True
            return 0
        print(f"daily batch start: asof={target.isoformat()} (business day)", flush=True)
    else:
        target = asof
        recorder.asof = target.isoformat()
        print(
            f"daily batch start: asof={target.isoformat()} (business-day gate skipped by --asof)",
            flush=True,
        )
    asof_arg = target.isoformat()
    edinet_quarantined_events = 0
    edinet_quarantined_tickers = 0
    edinet_quarantine_sample = "none"

    _run_step(
        runner,
        name="refresh-edinet-documents",
        argv=(_ENGINE, "screening", "refresh-edinet-documents", "--asof", asof_arg),
        cwd=root,
    )
    verify_argv = (_ENGINE, "screening", "verify-cache-coverage", "--asof", asof_arg)
    verify = _run_step(
        runner,
        name="verify-cache-coverage",
        argv=verify_argv,
        cwd=root,
        allowed_exit_codes=(0, 1),
    )
    if verify.returncode != 0 and _COVERAGE_INCOMPLETE_MARKER not in verify.stdout:
        raise BatchStepError(
            "verify-cache-coverage exited 1 without the coverage-incomplete marker; "
            "treating it as a crash (broken rules / unreadable store), not a cache gap\n"
            f"stderr (last {_STDERR_SUMMARY_LINES} lines):\n{_stderr_summary(verify.stderr)}",
            stage="verify-cache-coverage",
            returncode=verify.returncode,
        )
    # Coverage proves that a date was requested, not that a provider had already
    # published every filing for that date. Bootstrap always re-reads its bounded
    # financial-summary overlap so a later run can pick up delayed disclosures.
    _run_step(
        runner,
        name="bootstrap-cache",
        argv=(_ENGINE, "screening", "bootstrap-cache", "--asof", asof_arg),
        cwd=root,
    )
    # Document events are mutable throughout the day. Re-extract even when the
    # target-day snapshot already exists so a retry reports and stores the same
    # current quarantine state instead of publishing synthetic zero counters.
    extract_result = _run_step(
        runner,
        name="extract-edinet-metrics",
        argv=(_ENGINE, "screening", "extract-edinet-metrics", "--asof", asof_arg),
        cwd=root,
        echo_stdout_prefixes=("EDINET extraction summary: ",),
    )
    (
        edinet_quarantined_events,
        edinet_quarantined_tickers,
        edinet_quarantine_sample,
    ) = _parse_edinet_quarantine_metrics(extract_result.stdout)
    # The buyback authorisation state rides the same document list, but reads a
    # different form into a different table. It runs after the metric extraction so a
    # failure here never costs that extraction its work.
    _run_step(
        runner,
        name="refresh-buyback-reports",
        argv=(_ENGINE, "screening", "refresh-buyback-reports", "--asof", asof_arg),
        cwd=root,
        echo_stdout_prefixes=("refresh-buyback-reports: ",),
    )

    _run_step(runner, name="verify-cache-coverage(recheck)", argv=verify_argv, cwd=root)

    run_view = _run_screening_run(runner, root=root, asof_arg=asof_arg)

    select_argv: list[str] = [
        _ENGINE,
        "screening",
        "select",
        "--asof",
        asof_arg,
        "--run-revision-id",
        run_view.run_revision_id,
        # The longlist is the review input population, and the daily delta compares
        # it across runs. Publishing it every day keeps that comparison on the pool a
        # human would actually review instead of the cap-applied top-N.
        "--longlist-top",
        str(_SELECT_LONGLIST_TOP),
    ]
    try:
        previous_revision = previous_run_revision_id(root / _RUNS_DB_RELPATH, target)
    except sqlite3.Error as exc:
        raise BatchStepError(
            f"runs store is unreadable for previous-run resolution: {exc}",
            stage="screening-select",
            error_code="runs_store_unreadable",
        ) from exc
    if previous_revision is not None:
        select_argv.extend(("--previous-run-revision-id", previous_revision))
    select_result = _run_step(
        runner,
        name="screening-select",
        argv=select_argv,
        cwd=root,
        echo_stdout=False,
    )
    selection_view = _parse_selection_view(select_result.stdout)
    print(f"selection_id={selection_view.selection_id}", flush=True)

    recorder.batches.append(
        BatchResult(
            batch_name="screening",
            datasets=_BATCH_DATASETS["screening"],
            status=BATCH_STATUS_OK,
            duration_seconds=time.monotonic() - screening_mono,
            metrics={
                "asof": asof_arg,
                "run_revision_id": run_view.run_revision_id,
                "selection_id": selection_view.selection_id,
                "universe": run_view.universe_size,
                "candidates": run_view.candidate_count,
                "selected": selection_view.selected_count,
                "edinet_quarantined_events": edinet_quarantined_events,
                "edinet_quarantined_tickers": edinet_quarantined_tickers,
                "edinet_quarantine_sample": edinet_quarantine_sample,
            },
        )
    )

    # Macro series refresh must not block publishing the fresh screening result:
    # failures here are deferred to the final exit code after the export step.
    macro_mono = recorder.enter_section("macro")
    deferred_failures: list[str] = []
    macro_errors: list[BatchError] = []
    failed_series = 0

    def _record_deferred(exc: BatchStepError, errors: list[BatchError]) -> None:
        # Surface the failure detail immediately so it is not lost between here
        # and the final summary if a later step floods the log.
        print(f"deferred failure: {exc}", file=sys.stderr, flush=True)
        deferred_failures.append(str(exc))
        errors.append(_typed_error(exc, impact=ERROR_IMPACT_DEGRADED))

    refresh_groups: list[tuple[int, list[str]]] = []
    try:
        macro_list = _run_step(
            runner,
            name="macro-list",
            argv=(_ENGINE, "macro", "list", "--format", "json"),
            cwd=root,
            echo_stdout=False,
        )
        refresh_groups = _macro_refresh_groups(_parse_macro_series(macro_list.stdout))
    except BatchStepError as exc:
        _record_deferred(exc, macro_errors)
    for window_days, series_ids in refresh_groups:
        start = target - timedelta(days=window_days)
        try:
            _run_step(
                runner,
                name=f"macro-refresh-{window_days}d",
                argv=(
                    _ENGINE,
                    "macro",
                    "refresh",
                    *series_ids,
                    "--start",
                    start.isoformat(),
                    "--end",
                    asof_arg,
                ),
                cwd=root,
                echo_stdout=False,
                echo_stdout_prefixes=("registry-prune-pending\t", "registry-prune\t"),
            )
        except BatchStepError as exc:
            _record_deferred(exc, macro_errors)
            failed_series += _failed_series_count(exc, requested=len(series_ids))

    target_series = sum(len(series_ids) for _, series_ids in refresh_groups)
    recorder.batches.append(
        BatchResult(
            batch_name="macro",
            datasets=_BATCH_DATASETS["macro"],
            status=BATCH_STATUS_DEGRADED if macro_errors else BATCH_STATUS_OK,
            duration_seconds=time.monotonic() - macro_mono,
            metrics={
                "target": target_series,
                "success": target_series - failed_series,
                "failure": failed_series,
            },
            errors=tuple(macro_errors),
        )
    )

    export_mono = recorder.enter_section("serving-export")
    _run_step(
        runner,
        name="export-read-models",
        argv=(
            sys.executable,
            "-m",
            _EXPORT_MODULE,
            "--output-dir",
            str(output_dir),
            "--batch",
            "daily",
            "--repo-root",
            str(root),
        ),
        cwd=root,
    )
    recorder.local_export = (output_dir / "views" / "meta.json").is_file()
    recorder.batches.append(
        BatchResult(
            batch_name="serving-export",
            datasets=_BATCH_DATASETS["serving-export"],
            status=BATCH_STATUS_OK,
            duration_seconds=time.monotonic() - export_mono,
            metrics={
                "local_output": recorder.local_export,
                **_daily_delta_metrics(output_dir / "views" / "daily-delta.json"),
            },
        )
    )

    # Prune old run generations last so a prune hiccup never blocks the publish.
    prune_mono = recorder.enter_section("prune")
    prune_errors: list[BatchError] = []
    try:
        _run_step(runner, name="screening-prune", argv=(_ENGINE, "screening", "prune"), cwd=root)
    except BatchStepError as exc:
        _record_deferred(exc, prune_errors)
    recorder.batches.append(
        BatchResult(
            batch_name="prune",
            datasets=_BATCH_DATASETS["prune"],
            status=BATCH_STATUS_DEGRADED if prune_errors else BATCH_STATUS_OK,
            duration_seconds=time.monotonic() - prune_mono,
            metrics={},
            errors=tuple(prune_errors),
        )
    )

    # The exchange publishes its schedule only weeks ahead, so a follow-up task
    # created a quarter out carries an estimate until the real date enters that
    # window. Comparing daily is what surfaces the day it becomes knowable. It
    # reads nothing the publish produced and writes nothing, so it runs after the
    # publish and a failure degrades rather than blocking it. It gets its own
    # section: folding it into prune would report prune as degraded when prune
    # succeeded, and mark the runs store degraded when the runs store is fine.
    reconcile_mono = recorder.enter_section("task-reconcile")
    reconcile_errors: list[BatchError] = []
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
        )
    except BatchStepError as exc:
        _record_deferred(exc, reconcile_errors)
    recorder.batches.append(
        BatchResult(
            batch_name="task-reconcile",
            datasets=_BATCH_DATASETS["task-reconcile"],
            status=BATCH_STATUS_DEGRADED if reconcile_errors else BATCH_STATUS_OK,
            duration_seconds=time.monotonic() - reconcile_mono,
            metrics={},
            errors=tuple(reconcile_errors),
        )
    )

    print(
        "daily batch done: "
        f"asof={asof_arg}; run_revision_id={run_view.run_revision_id}; "
        f"selection_id={selection_view.selection_id}",
        flush=True,
    )
    if deferred_failures:
        print(
            f"error: export published, but {len(deferred_failures)} deferred step(s) failed:",
            file=sys.stderr,
        )
        for failure in deferred_failures:
            print(f"- {failure}", file=sys.stderr)
        return _EXIT_DEFERRED_FAILURE
    return 0


def _root_error(root: Path) -> str | None:
    return repository_root_error(root, label="--repo-root")


def _write_invalid_asof_summary(summary_output: Path | None, raw_asof: str) -> None:
    """Write a fatal summary for an unparseable ``--asof`` before the batch starts."""

    if summary_output is None:
        return
    sanitized_asof = " ".join(raw_asof.split())[:80] or "<empty>"
    now = datetime.now(_JST)
    error = BatchError.build(
        code="invalid_asof", stage="input", impact=ERROR_IMPACT_FAILED, value=raw_asof
    )
    failed = BatchResult(
        batch_name="screening",
        datasets=_BATCH_DATASETS["screening"],
        status=BATCH_STATUS_FAILED,
        duration_seconds=0.0,
        metrics={},
        errors=(error,),
    )
    summary = BatchExecutionSummary(
        schema_version=BATCH_SUMMARY_SCHEMA_VERSION,
        asof=sanitized_asof,
        outcome=OUTCOME_FAILED,
        started_at=now.isoformat(),
        finished_at=now.isoformat(),
        duration_seconds=0.0,
        batches=(failed,),
        local_export=False,
    )
    validated = BatchExecutionSummary.from_json(summary.to_json())
    write_json_atomic(summary_output, validated.to_json())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_batch",
        description=(
            "run the daily machine batch in one command: business-day gate -> "
            "screening cache coverage/run/select -> macro refresh -> "
            "read-model export"
        ),
    )
    parser.add_argument(
        "--asof",
        type=str,
        help=(
            "run for this date (YYYY-MM-DD) and skip the business-day gate "
            "(manual rerun / past date); default is today in JST, gated by the market calendar"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="serving read-model output directory passed to export_read_models",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=None,
        help="write a structured BatchExecutionSummary JSON to this path on every terminal path",
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
    summary_output = args.summary_output.resolve() if args.summary_output is not None else None
    # Validate --asof only after the summary output path is resolved so an invalid
    # input still produces a fatal summary instead of an argparse abort.
    asof: date | None = None
    if args.asof is not None:
        try:
            asof = date.fromisoformat(args.asof)
        except ValueError:
            _write_invalid_asof_summary(summary_output, args.asof)
            print(f"error: asof '{args.asof}' is not a valid YYYY-MM-DD date", file=sys.stderr)
            return 1
    try:
        return run_daily_batch(
            root=root,
            output_dir=args.output_dir.resolve(),
            asof=asof,
            runner=_run_subprocess,
            summary_output=summary_output,
        )
    except (BatchStepError, CalendarCoverageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
