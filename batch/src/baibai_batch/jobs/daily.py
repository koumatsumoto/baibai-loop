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

from baibai_batch.observability.discord import sanitize_one_line, write_json_atomic
from baibai_engine.batch_api import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
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
        # failure policy: 1 — the business-day decision has no required calendar store.
        raise CalendarCoverageError(f"market SQLite does not exist: {market_db}")
    try:
        result = market_calendar_business_day(market_db, day)
    except sqlite3.Error as exc:
        # failure policy: 1 — an unreadable calendar cannot supply the required row.
        raise CalendarCoverageError(f"market calendar is unreadable in {market_db}: {exc}") from exc
    if result is None:
        # failure policy: 1 — the calendar has no row for the target date.
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
) -> CommandResult:
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
            stage=name,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def _finite_number(value: object) -> float | None:
    """A JSON number that can be rendered, or None. ``bool`` is not a number here."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _delta_ticker_labels(rows: object) -> list[str]:
    """Name the tickers on one side of the pool delta, best estimate first.

    A day that moves names into or out of the pool is worth acting on that evening,
    and a count alone does not say which names to look at. The list is capped
    because the reader acts on the top of it. The full entry sets remain in the
    exported daily-delta view; the notice only carries the names worth opening.

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


def _read_daily_delta(path: Path, notice: _Notice) -> None:
    """Carry the day's longlist entries and exits into the notice.

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
    candidate_count: int


def _read_run_view(run_yaml: Path) -> _RunView:
    """Read run_revision_id + universe/candidate counts from the run YAML view.

    ``screening run --output-path`` writes ``universe_size`` and ``candidates``
    alongside ``run_revision_id``; the counts default to 0 when a view omits them
    so a minimal view still surfaces the stable ``run_revision_id`` contract.
    """

    if not run_yaml.is_file():
        # failure policy: 2 — select cannot bind to a run without its publication ID.
        raise BatchStepError(
            "screening run did not write the --output-path YAML view",
            stage="screening-run",
        )
    try:
        payload = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        # failure policy: 2 — an unreadable run view cannot safely identify select input.
        raise BatchStepError(
            f"screening run YAML view is unreadable: {exc}",
            stage="screening-run",
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
    # failure policy: 2 — select would otherwise point at an unknown run publication.
    raise BatchStepError(
        "screening run YAML view does not contain run_revision_id",
        stage="screening-run",
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
        # failure policy: 2 — an unreadable selection result cannot identify the publish.
        raise BatchStepError(
            f"screening select output is not parseable YAML: {exc}",
            stage="screening-select",
        ) from exc
    if isinstance(payload, dict):
        selection_id = payload.get("selection_id")
        if isinstance(selection_id, str) and selection_id:
            recommendations = payload.get("recommendations")
            selected_count = len(recommendations) if isinstance(recommendations, list) else 0
            return _SelectionView(selection_id, selected_count)
    # failure policy: 2 — continuing would export a result with no selection identity.
    raise BatchStepError(
        "screening select output does not contain selection_id",
        stage="screening-select",
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


@dataclass(slots=True)
class _Notice:
    """What the batch leaves for the Discord notifier on every terminal path.

    The as-of it ran for, whether the business-day gate skipped it, the first
    fatal or deferred failure stage, and the names that entered or left the
    longlist. The exit code carries the outcome itself.
    """

    asof: str = ""
    skipped: bool = False
    failed_stage: str | None = None
    delta_measured: bool = False
    delta_unmeasured_reason: str = "view_unreadable"
    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "asof": self.asof,
            "skipped": self.skipped,
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
    notice_output: Path | None = None,
) -> int:
    notice = _Notice()
    try:
        exit_code = _execute_daily_batch(
            root=root, output_dir=output_dir, asof=asof, runner=runner, notice=notice
        )
    except (BatchStepError, CalendarCoverageError) as exc:
        notice.failed_stage = exc.stage or "batch"
        _write_notice(notice_output, notice)
        raise
    _write_notice(notice_output, notice)
    return exit_code


def _execute_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
    notice: _Notice,
) -> int:
    if asof is None:
        target = datetime.now(_JST).date()
        notice.asof = target.isoformat()
        if not _require_business_day(root / _MARKET_DB_RELPATH, target):
            print(f"skip: {target.isoformat()} は非営業日", flush=True)
            notice.skipped = True
            return 0
        print(f"daily batch start: asof={target.isoformat()} (business day)", flush=True)
    else:
        target = asof
        notice.asof = target.isoformat()
        print(
            f"daily batch start: asof={target.isoformat()} (business-day gate skipped by --asof)",
            flush=True,
        )
    asof_arg = target.isoformat()

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
        # failure policy: 2 — an unclassified verifier crash cannot prove safe input.
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
    _parse_edinet_quarantine_metrics(extract_result.stdout)
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
        # failure policy: 2 — select cannot bind its comparison to an unreadable store.
        raise BatchStepError(
            f"runs store is unreadable for previous-run resolution: {exc}",
            stage="screening-select",
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

    # Macro series refresh must not block publishing the fresh screening result:
    # failures here are deferred to the final exit code after the export step.
    deferred_failures: list[str] = []

    def _record_deferred(exc: BatchStepError) -> None:
        # Surface the failure detail immediately so it is not lost if a later
        # step floods the log.
        print(f"deferred failure: {exc}", file=sys.stderr, flush=True)
        if notice.failed_stage is None:
            notice.failed_stage = exc.stage or "batch"
        deferred_failures.append(str(exc))

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
        _record_deferred(exc)
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
            _record_deferred(exc)

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
    _read_daily_delta(output_dir / "views" / "daily-delta.json", notice)

    # Prune old run generations last so a prune hiccup never blocks the publish.
    try:
        _run_step(runner, name="screening-prune", argv=(_ENGINE, "screening", "prune"), cwd=root)
    except BatchStepError as exc:
        _record_deferred(exc)

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
        )
    except BatchStepError as exc:
        _record_deferred(exc)

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
        "--notice-output",
        type=Path,
        default=None,
        help="write the JSON notice the Discord notifier reads to this path on every terminal path",
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
        return run_daily_batch(
            root=root,
            output_dir=args.output_dir.resolve(),
            asof=asof,
            runner=_run_subprocess,
            notice_output=notice_output,
        )
    except (BatchStepError, CalendarCoverageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
