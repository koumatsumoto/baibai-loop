"""Run the local daily machine batch as one command.

Order: business-day gate -> screening cache coverage (bootstrap on demand) ->
``screening run`` -> ``screening select`` -> macro series refresh +
``import-manual`` -> read-model export -> run-store prune. Every heavy step goes
through the public ``baibai-engine`` CLI (or the export script) as a subprocess,
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
import sqlite3
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.read_api import market_calendar_business_day, previous_run_revision_id

_JST = ZoneInfo("Asia/Tokyo")
_ENGINE = "baibai-engine"
_MARKET_DB_RELPATH = Path("data/screening/market.sqlite")
_RUNS_DB_RELPATH = Path("data/screening/runs.sqlite")
_EXPORT_SCRIPT_RELPATH = Path("tools/cloud/export_read_models.py")
# Refresh window per series frequency, mirroring the provider re-fetch windows
# the macro `get --latest` freshness check uses (daily 14 / weekly 60 /
# monthly and slower 370 calendar days).
_MACRO_REFRESH_WINDOW_DAYS = {"daily": 14, "weekly": 60}
_MACRO_REFRESH_WINDOW_DEFAULT_DAYS = 370
_STDERR_SUMMARY_LINES = 20
# `verify-cache-coverage` prints this marker on stdout for a genuine cache gap;
# an exit 1 without it (broken rules, unreadable store) is a crash, not a gap.
_COVERAGE_INCOMPLETE_MARKER = "SQLite cache coverage incomplete"
# Exit 3 means the screening result was published and exported, but a deferred
# (macro / prune) step failed afterwards.
_EXIT_DEFERRED_FAILURE = 3


class BatchStepError(RuntimeError):
    """A batch step failed or produced output the chain cannot continue from."""


class CalendarCoverageError(RuntimeError):
    """The market calendar cannot answer the business-day question for a date."""


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
        raise CalendarCoverageError(f"market SQLite does not exist: {market_db}")
    try:
        result = market_calendar_business_day(market_db, day)
    except sqlite3.Error as exc:
        raise CalendarCoverageError(f"market calendar is unreadable in {market_db}: {exc}") from exc
    if result is None:
        raise CalendarCoverageError(
            f"market calendar does not cover {day.isoformat()}; "
            "refresh the calendar cache before running the daily batch"
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
) -> CommandResult:
    print(f"$ {' '.join(argv)}", flush=True)
    started = time.monotonic()
    try:
        result = runner(argv, cwd)
    except FileNotFoundError as exc:
        raise BatchStepError(f"step {name}: command not found: {argv[0]}") from exc
    elapsed = time.monotonic() - started
    if echo_stdout and result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    print(f"step {name}: exit {result.returncode} ({elapsed:.1f}s)", flush=True)
    if result.returncode not in allowed_exit_codes:
        raise BatchStepError(
            f"step {name} failed with exit {result.returncode}\n"
            f"stderr (last {_STDERR_SUMMARY_LINES} lines):\n{_stderr_summary(result.stderr)}"
        )
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def _stderr_summary(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    if not lines:
        return "  (empty)"
    return "\n".join(f"  {line}" for line in lines[-_STDERR_SUMMARY_LINES:])


def _read_run_revision_id(run_yaml: Path) -> str:
    """Read run_revision_id from the stable YAML view `screening run --output-path` writes."""

    if not run_yaml.is_file():
        raise BatchStepError("screening run did not write the --output-path YAML view")
    try:
        payload = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BatchStepError(f"screening run YAML view is unreadable: {exc}") from exc
    if isinstance(payload, dict):
        run_revision_id = payload.get("run_revision_id")
        if isinstance(run_revision_id, str) and run_revision_id:
            return run_revision_id
    raise BatchStepError("screening run YAML view does not contain run_revision_id")


def _parse_selection_id(stdout: str) -> str:
    try:
        payload = yaml.safe_load(stdout)
    except yaml.YAMLError as exc:
        raise BatchStepError(f"screening select output is not parseable YAML: {exc}") from exc
    if isinstance(payload, dict):
        selection_id = payload.get("selection_id")
        if isinstance(selection_id, str) and selection_id:
            return selection_id
    raise BatchStepError("screening select output does not contain selection_id")


@dataclass(frozen=True, slots=True)
class _MacroSeries:
    series_id: str
    frequency: str
    provider: str


def _parse_macro_series(stdout: str) -> list[_MacroSeries]:
    series: list[_MacroSeries] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise BatchStepError(
                f"macro list line is not the expected 7 tab-separated fields: {line!r}"
            )
        series.append(_MacroSeries(series_id=fields[0], frequency=fields[4], provider=fields[6]))
    return series


def _macro_refresh_groups(series: Sequence[_MacroSeries]) -> list[tuple[int, list[str]]]:
    """Group refreshable series by window days; manual series only sync via import-manual."""

    groups: dict[int, list[str]] = {}
    for item in series:
        if item.provider == "manual":
            continue
        window = _MACRO_REFRESH_WINDOW_DAYS.get(item.frequency, _MACRO_REFRESH_WINDOW_DEFAULT_DAYS)
        groups.setdefault(window, []).append(item.series_id)
    return sorted(groups.items())


def _run_screening_run(runner: CommandRunner, *, root: Path, asof_arg: str) -> str:
    """Run screening and read run_revision_id from the YAML view it writes.

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
        return _read_run_revision_id(run_yaml)


def run_daily_batch(
    *,
    root: Path,
    output_dir: Path,
    asof: date | None,
    runner: CommandRunner,
) -> int:
    if asof is None:
        target = datetime.now(_JST).date()
        if not _require_business_day(root / _MARKET_DB_RELPATH, target):
            print(f"skip: {target.isoformat()} は非営業日", flush=True)
            return 0
        print(f"daily batch start: asof={target.isoformat()} (business day)", flush=True)
    else:
        target = asof
        print(
            f"daily batch start: asof={target.isoformat()} (business-day gate skipped by --asof)",
            flush=True,
        )
    asof_arg = target.isoformat()

    verify_argv = (_ENGINE, "screening", "verify-cache-coverage", "--asof", asof_arg)
    verify = _run_step(
        runner,
        name="verify-cache-coverage",
        argv=verify_argv,
        cwd=root,
        allowed_exit_codes=(0, 1),
    )
    if verify.returncode != 0:
        if _COVERAGE_INCOMPLETE_MARKER not in verify.stdout:
            raise BatchStepError(
                "verify-cache-coverage exited 1 without the coverage-incomplete marker; "
                "treating it as a crash (broken rules / unreadable store), not a cache gap\n"
                f"stderr (last {_STDERR_SUMMARY_LINES} lines):\n{_stderr_summary(verify.stderr)}"
            )
        _run_step(
            runner,
            name="bootstrap-cache",
            argv=(_ENGINE, "screening", "bootstrap-cache", "--asof", asof_arg),
            cwd=root,
        )
        _run_step(
            runner,
            name="extract-edinet-metrics",
            argv=(_ENGINE, "screening", "extract-edinet-metrics", "--asof", asof_arg),
            cwd=root,
        )
        _run_step(runner, name="verify-cache-coverage(recheck)", argv=verify_argv, cwd=root)

    run_revision_id = _run_screening_run(runner, root=root, asof_arg=asof_arg)

    select_argv: list[str] = [
        _ENGINE,
        "screening",
        "select",
        "--asof",
        asof_arg,
        "--run-revision-id",
        run_revision_id,
    ]
    try:
        previous_revision = previous_run_revision_id(root / _RUNS_DB_RELPATH, target)
    except sqlite3.Error as exc:
        raise BatchStepError(
            f"runs store is unreadable for previous-run resolution: {exc}"
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
    selection_id = _parse_selection_id(select_result.stdout)
    print(f"selection_id={selection_id}", flush=True)

    # Macro series refresh must not block publishing the fresh screening result:
    # failures here are deferred to the final exit code after the export step.
    deferred_failures: list[str] = []

    def _record_deferred(exc: BatchStepError) -> None:
        # Surface the failure detail immediately so it is not lost between here
        # and the final summary if a later step floods the log.
        print(f"deferred failure: {exc}", file=sys.stderr, flush=True)
        deferred_failures.append(str(exc))

    refresh_groups: list[tuple[int, list[str]]] = []
    try:
        macro_list = _run_step(
            runner,
            name="macro-list",
            argv=(_ENGINE, "macro", "list"),
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
            )
        except BatchStepError as exc:
            _record_deferred(exc)
    try:
        _run_step(
            runner, name="macro-import-manual", argv=(_ENGINE, "macro", "import-manual"), cwd=root
        )
    except BatchStepError as exc:
        _record_deferred(exc)

    _run_step(
        runner,
        name="export-read-models",
        argv=(
            sys.executable,
            str(root / _EXPORT_SCRIPT_RELPATH),
            "--output-dir",
            str(output_dir),
            "--batch",
            "daily",
            "--repo-root",
            str(root),
        ),
        cwd=root,
    )
    # Prune old run generations last so a prune hiccup never blocks the publish.
    try:
        _run_step(runner, name="screening-prune", argv=(_ENGINE, "screening", "prune"), cwd=root)
    except BatchStepError as exc:
        _record_deferred(exc)

    print(
        "daily batch done: "
        f"asof={asof_arg}; run_revision_id={run_revision_id}; selection_id={selection_id}",
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
    if not (root / "pyproject.toml").is_file():
        return f"--repo-root does not contain pyproject.toml: {root}"
    if not (root / "records").is_dir():
        return f"--repo-root does not contain records/: {root}"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_batch",
        description=(
            "run the daily machine batch in one command: business-day gate -> "
            "screening cache coverage/run/select -> macro refresh + import-manual -> "
            "read-model export"
        ),
    )
    parser.add_argument(
        "--asof",
        type=date.fromisoformat,
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
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    error = _root_error(root)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        return run_daily_batch(
            root=root,
            output_dir=args.output_dir.resolve(),
            asof=args.asof,
            runner=_run_subprocess,
        )
    except (BatchStepError, CalendarCoverageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
