"""市場・経済取得と共通統合の実行欠落を、対象日とrun結果から通知する。

各jobの通知だけでは起動しなかった実行を検出できないため、gh APIの履歴を読む。
確認範囲に対象日の成功も進行中runもなければ同じ#batch-runsへ通知する。
dailyの既知対象日で長時間queuedのままなら、起動欠落とは別に待機遅延を通知する。
成功と進行中は区別し、遅延・欠落の原因や通知配送の成功を推定しない。
取得・再実行・公開は行わず、healthy/in_flight時は重複した正常通知を送らない。

runnerのsystem python3でuvなしに動くよう、標準libraryだけで3.13+構文を使わない。
"""

from __future__ import annotations

import argparse
import json
import os

# fixed read-only gh API command
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_jobs.observability.discord import (
    DEFAULT_TIMEOUT_SECONDS,
    WEBHOOK_ENV_VAR,
    Transport,
    _urllib_transport,
    deliver,
    render_run_notice,
    run_url,
    sanitize_one_line,
)
from baibai_jobs.schedule import (
    MARKET_SOURCE_SCHEDULES,
    acquisition_target_date,
    batch_target_date,
    integration_target_date,
    market_source_target_date,
)

WATCHED_WORKFLOW = "cloud-daily-batch"
RUNS_SHOWN = 5

# The watchdog's own cron, in UTC. `--check-date` evaluates the window a scheduled
# firing on that date would have used, which is what makes a past day reproducible.
SCHEDULED_FIRE_TIME = time(16, 0, tzinfo=UTC)
ACQUISITION_WATCHDOG_FIRE_TIME = time(12, 0, tzinfo=UTC)

# Bound the run-history window; a matching reported target date, not recency
# alone, decides whether a successful run answers the scheduled day.
DEFAULT_WINDOW_HOURS = 20
# Queue age, not execution time: a long wait can precede a healthy full-length job.
# Use the daily job's existing 210-minute budget without adding a new grace period.
DAILY_QUEUED_ALERT_AFTER = timedelta(minutes=210)

# In-flight runs use explicit recovery inputs or the original run clock.
# Success is bound to the notice as-of carried by the actual terminal step result.
RESULT_STEP_PREFIX = "Notify Discord #batch-runs for "
MARKET_SCOPE_SCHEDULES = {
    "inputs": "43 7 * * *",
    "edinet-metrics": "43 8 * * *",
    "financial-edinet-facts": "13 9 * * *",
}


class WatchdogInputError(ValueError):
    """The run listing does not satisfy the shape this adapter reads."""


def run_target_date(*, event: str, display_title: str, created_at: datetime) -> date | None:
    """Resolve known in-flight runs without advancing an old run on rerun."""

    if event == "workflow_dispatch":
        match display_title.split():
            case ["daily", day] | ["economic", _, _, day] | ["market-acquisition", _, day]:
                try:
                    return date.fromisoformat(day)
                except ValueError:
                    return None
            case ["daily"] | ["economic", _, _] | ["market-acquisition", _]:
                return created_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
        return None
    if event == "schedule":
        match display_title.split():
            case ["daily"] | ["cloud-daily-batch"]:
                return integration_target_date(created_at, scheduled=True)
            case ["economic", "all", "all"]:
                return acquisition_target_date(created_at)
            case ["market-acquisition", scope] if scope in MARKET_SCOPE_SCHEDULES:
                return market_source_target_date(created_at, schedule=MARKET_SCOPE_SCHEDULES[scope])
    return None


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One run of the watched workflow, reduced to the fields the verdict uses."""

    run_number: int
    event: str
    status: str
    conclusion: str
    created_at: datetime
    updated_at: datetime
    target_date: date | None
    run_id: int
    run_attempt: int
    display_title: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.conclusion == "success"

    @property
    def in_flight(self) -> bool:
        return self.status != "completed"

    def describe(self) -> str:
        state = self.conclusion or self.status
        target = self.target_date.isoformat() if self.target_date is not None else "unnamed day"
        return (
            f"#{self.run_number} {sanitize_one_line(self.event)} "
            f"{sanitize_one_line(state)} {self.updated_at.isoformat()} for {target}"
        )


STATE_HEALTHY = "healthy"
STATE_IN_FLIGHT = "in_flight"
STATE_MISSING = "missing"
STATE_DELAYED = "delayed"
STATE_PUBLISHED_QUALITY = "published_source_quality"

TV_QUALITY_SUFFIX = " [fixed TradingView receipt quality]"
SOURCE_CHECK_STEP = "Report integration failures and deferred source inputs"
CANCELLATION_STEP = "Record cancellation"
RETIREMENT_JOB = "retirement / check"
RETIREMENT_PROOF_STEP = "Verify the event checkout and retired writer run inventory"
PUBLISHED_STEPS = {
    "Acquire publication lease",
    "Apply fixed original receipts and common offline extraction",
    "Generate models and candidates, then publish the same fixed release",
    "Upload the core machine stores",
    "Seal canonical market handoffs",
    "Refresh source-scoped inputs from this canonical generation",
    "Report retained economic source failures",
    "Publish retained portfolio views",
    "Release publication lease",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the window holds, and therefore whether the batch is missing.

    ``in_flight`` is separate from ``missing``: a late-started batch is not absent.
    This observation does not prove eventual completion or notification delivery;
    cancellation, timeout or transport failure can prevent a terminal notice.
    Treating every running batch as missing would create false alarms whenever
    the schedule queue delays integration past the watchdog.
    """

    state: str
    check_date: date
    window_start: datetime
    window_end: datetime
    runs_in_window: tuple[WorkflowRun, ...]
    delayed_runs: tuple[WorkflowRun, ...] = ()
    quality_run: WorkflowRun | None = None

    @property
    def alerting(self) -> bool:
        return self.state in {STATE_MISSING, STATE_DELAYED, STATE_PUBLISHED_QUALITY}


def _parse_instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise WatchdogInputError(f"run {field} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WatchdogInputError(f"run {field} is not an ISO instant") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_runs(payload: object) -> tuple[WorkflowRun, ...]:
    """Read the ``workflow_runs`` array of a ``gh api`` listing.

    A malformed listing raises rather than returning an empty tuple: "no runs" is
    the alert condition, so a parse that silently degrades to it would turn every
    API shape change into a false alarm.
    """
    if not isinstance(payload, Mapping):
        raise WatchdogInputError("run listing must be a JSON object")
    raw_runs = payload.get("workflow_runs")
    if not isinstance(raw_runs, Sequence) or isinstance(raw_runs, (str, bytes)):
        raise WatchdogInputError("run listing must carry a workflow_runs array")
    runs: list[WorkflowRun] = []
    for entry in raw_runs:
        if not isinstance(entry, Mapping):
            raise WatchdogInputError("each workflow run must be a JSON object")
        run_number = entry.get("run_number")
        if not isinstance(run_number, int) or isinstance(run_number, bool):
            raise WatchdogInputError("run run_number must be an integer")
        event = str(entry.get("event", "unknown"))
        created_at = _parse_instant(entry.get("created_at"), field="created_at")
        updated_at = _parse_instant(entry.get("updated_at"), field="updated_at")
        status = str(entry.get("status", "unknown"))
        display_title = str(entry.get("display_title") or "")
        target = None
        actual = entry.get("batch_asof")
        if actual is not None:
            if not isinstance(actual, str):
                raise WatchdogInputError("batch as-of must be a YYYY-MM-DD string")
            try:
                target = date.fromisoformat(actual)
                if target.isoformat() != actual:
                    raise ValueError
            except ValueError as exc:
                raise WatchdogInputError("batch as-of must be a YYYY-MM-DD string") from exc
        elif status != "completed":
            target = run_target_date(
                event=event,
                display_title=display_title,
                created_at=created_at,
            )
        run_id, attempt = entry.get("id"), entry.get("run_attempt")
        if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
            raise WatchdogInputError("run id and attempt must be positive integers")
        runs.append(
            WorkflowRun(
                run_number=run_number,
                event=event,
                status=status,
                conclusion=str(entry.get("conclusion") or ""),
                created_at=created_at,
                updated_at=updated_at,
                target_date=target,
                run_id=run_id,
                run_attempt=attempt,
                display_title=display_title,
            )
        )
    return tuple(runs)


JobsReader = Callable[[str, int, int], object]


def _read_attempt_jobs(
    repository: str, run_id: int, attempt: int, *, timeout: float = 30
) -> object:
    """Read step results from the listed attempt, never from another rerun."""
    try:
        # fixed gh read with validated integer ids
        result = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "api",
                f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise WatchdogInputError("cannot read daily attempt results") from exc


_read_quality_attempt_jobs: JobsReader = partial(_read_attempt_jobs, timeout=5)


def resolve_run_results(
    runs: Sequence[WorkflowRun],
    *,
    repository: str,
    window_end: datetime,
    window_hours: int,
    read_jobs: JobsReader,
    result_prefix: str = RESULT_STEP_PREFIX,
    repeated_source_results: bool = False,
) -> tuple[WorkflowRun, ...]:
    """Use the batch's reported target from recent successful attempt results."""
    resolved: list[WorkflowRun] = []
    for run in runs:
        if (
            not run.succeeded
            or not window_end - timedelta(hours=window_hours) <= run.updated_at <= window_end
        ):
            resolved.append(run)
            continue
        payload = read_jobs(repository, run.run_id, run.run_attempt)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
            raise WatchdogInputError("attempt results must carry a jobs array")
        targets: list[date] = []
        for job in payload["jobs"]:
            if not isinstance(job, Mapping) or not isinstance(job.get("steps"), list):
                raise WatchdogInputError("attempt job must carry a steps array")
            for step in job["steps"]:
                if not isinstance(step, Mapping):
                    raise WatchdogInputError("attempt step must be an object")
                name = str(step.get("name") or "")
                if name.startswith(result_prefix) and step.get("status") == "completed":
                    value = name[len(result_prefix) :]
                    try:
                        target = date.fromisoformat(value)
                        if target.isoformat() != value:
                            raise ValueError
                        targets.append(target)
                    except ValueError:
                        pass
        if repeated_source_results:
            targets = sorted(set(targets))
        if len(targets) != 1:
            raise WatchdogInputError("successful daily attempt has no unique reported as-of")
        resolved.append(replace(run, target_date=targets[0]))
    return tuple(resolved)


def evaluate(
    runs: Sequence[WorkflowRun],
    *,
    window_end: datetime,
    window_hours: int,
    target_date: date | None = None,
    queued_delay_after: timedelta | None = None,
) -> Verdict:
    """Decide whether the window holds a completed, successful run."""
    if window_hours <= 0:
        raise WatchdogInputError("window hours must be positive")
    window_start = window_end - timedelta(hours=window_hours)
    in_window = tuple(
        sorted(
            (run for run in runs if window_start <= run.updated_at <= window_end),
            key=lambda run: run.updated_at,
        )
    )
    check_date = target_date or batch_target_date(window_end)
    # A recovery dispatch for an earlier day says nothing about this one. Reading the
    # window as a set of runs rather than as a set of answered days is what let a
    # past-dated success — the shape that actually occurred on 2026-08-18 — cover a
    # missing batch. The same filter applies to a run still in flight for the same
    # reason: it will report an outcome about its own day, not about this one.
    answering = tuple(run for run in in_window if run.target_date == check_date)
    delayed = tuple(
        run
        for run in answering
        if queued_delay_after is not None
        and run.status == "queued"
        # Reruns keep the original run clock, not this attempt's queue start.
        and run.run_attempt == 1
        and window_end - run.created_at > queued_delay_after
    )
    if any(run.succeeded for run in answering):
        state = STATE_HEALTHY
    elif delayed:
        state = STATE_DELAYED
    elif any(run.in_flight and run.target_date in {None, check_date} for run in in_window):
        state = STATE_IN_FLIGHT
    else:
        state = STATE_MISSING
    return Verdict(
        state=state,
        check_date=check_date,
        window_start=window_start,
        window_end=window_end,
        runs_in_window=in_window,
        delayed_runs=delayed if state == STATE_DELAYED else (),
    )


def _iso_day(value: str) -> date | None:
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value else None


def _completed_attempt_steps(
    run: WorkflowRun, job: Mapping[str, object], *, conclusion: str
) -> dict[str, str] | None:
    steps = job.get("steps")
    if (
        not isinstance(steps, list)
        or type(job.get("run_id")) is not int
        or job["run_id"] != run.run_id
        or type(job.get("run_attempt")) is not int
        or job["run_attempt"] != run.run_attempt
        or job.get("status") != "completed"
        or job.get("conclusion") != conclusion
    ):
        return None
    results: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            return None
        name, outcome = step.get("name"), step.get("conclusion")
        if (
            not isinstance(name, str)
            or name in results
            or step.get("status") != "completed"
            or outcome not in ("success", "skipped", "failure")
        ):
            return None
        results[name] = outcome
    return results


def _failed_daily_result(run: WorkflowRun, payload: object) -> tuple[date, bool] | None:
    """Return the date and whether both successor jobs prove a quality-only failure."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        return None
    jobs = payload["jobs"]
    expected = {RETIREMENT_JOB: "success", "daily": "failure"}
    if (
        len(jobs) != len(expected)
        or type(payload.get("total_count")) is not int
        or payload["total_count"] != len(expected)
    ):
        return None
    by_job: dict[str, dict[str, str]] = {}
    for job in jobs:
        name = job.get("name") if isinstance(job, Mapping) else None
        if not isinstance(name, str) or name not in expected or name in by_job:
            return None
        if (steps := _completed_attempt_steps(run, job, conclusion=expected[name])) is None:
            return None
        by_job[name] = steps
    proof = by_job[RETIREMENT_JOB]
    if proof.get(RETIREMENT_PROOF_STEP) != "success" or "failure" in proof.values():
        return None
    results = by_job["daily"]
    notices = [name for name in results if name.startswith(RESULT_STEP_PREFIX)]
    if (
        len(notices) != 1
        or results[notices[0]] != "success"
        or results.get(CANCELLATION_STEP) != "skipped"
    ):
        return None
    value = notices[0][len(RESULT_STEP_PREFIX) :]
    day = _iso_day(value.removesuffix(TV_QUALITY_SUFFIX))
    if day is None:
        return None
    return day, (
        value.endswith(TV_QUALITY_SUFFIX)
        and all(results.get(name) == "success" for name in PUBLISHED_STEPS)
        and [name for name, status in results.items() if status == "failure"] == [SOURCE_CHECK_STEP]
    )


def _pins_another_daily_date(run: WorkflowRun, target: date) -> bool:
    match run.event, run.display_title.split():
        case "workflow_dispatch", ["daily", value]:
            pinned = _iso_day(value)
            return pinned is not None and pinned != target
    return False


def _resolve_missing_daily(verdict: Verdict, repository: str, read_jobs: JobsReader) -> Verdict:
    candidates = (
        run
        for run in reversed(verdict.runs_in_window)
        if not (run.succeeded or run.in_flight or _pins_another_daily_date(run, verdict.check_date))
    )
    inspected = 0
    for run in islice(candidates, 2):
        inspected += 1
        if run.conclusion != "failure":
            break  # A newer cancellation/unknown terminal may already have written.
        try:
            evidence = _failed_daily_result(run, read_jobs(repository, run.run_id, run.run_attempt))
        except WatchdogInputError:
            evidence = None
        if evidence == (verdict.check_date, True):
            return replace(verdict, state=STATE_PUBLISHED_QUALITY, quality_run=run)
        if evidence is None or evidence[0] == verdict.check_date:
            break  # Only a positively different date can make an older attempt relevant.
    if inspected:
        print(
            f"watchdog: no conclusive daily quality evidence in {inspected} "
            "terminal attempt(s); keeping MISSING",
            file=sys.stderr,
        )
    return verdict


def render_alert(
    verdict: Verdict,
    *,
    repository: str,
    watchdog_run_url: str,
    workflow: str = WATCHED_WORKFLOW,
) -> str:
    """Render the bounded alert message for a window with no successful run."""
    if verdict.quality_run is not None:
        run = verdict.quality_run
        return render_run_notice(
            label="[SOURCE QUALITY]",
            batch="実行監視（cloud-batch-watchdog）",
            asof=verdict.check_date.isoformat(),
            details=[
                "日次本体は公開済み。失敗したのは個社TradingViewの固定受領品質チェックのみ。",
                "この受領失敗だけを理由に日次全体は再実行しない。他の取得の完全性は未確認。",
                (
                    f"対象attempt: https://github.com/{sanitize_one_line(repository)}"
                    f"/actions/runs/{run.run_id}/attempts/{run.run_attempt}"
                ),
            ],
            url=watchdog_run_url,
        )
    window_hours = int((verdict.window_end - verdict.window_start).total_seconds() // 3600)
    lines = [
        f"監視対象: {sanitize_one_line(workflow)}",
        f"直近{window_hours}時間の確認範囲に対象日の成功runなし",
        f"repo: {sanitize_one_line(repository)}",
        f"window: {verdict.window_start.isoformat()} .. {verdict.window_end.isoformat()}",
    ]
    if verdict.state == STATE_DELAYED:
        lines.append("queuedの待機が210分を超過。実行停止や遅延原因は未確認")
        for run in verdict.delayed_runs[:RUNS_SHOWN]:
            minutes = int((verdict.window_end - run.created_at).total_seconds() // 60)
            lines.append(
                f"#{run.run_number} queued age={minutes}min: "
                f"https://github.com/{sanitize_one_line(repository)}/actions/runs/{run.run_id}"
            )
    if verdict.runs_in_window:
        lines.append("runs in window:")
        lines.extend(run.describe() for run in verdict.runs_in_window[:RUNS_SHOWN])
        if len(verdict.runs_in_window) > RUNS_SHOWN:
            lines.append(f"+{len(verdict.runs_in_window) - RUNS_SHOWN} more")
    else:
        lines.append("runs in window: none")
    return render_run_notice(
        label="[DELAYED]" if verdict.state == STATE_DELAYED else "[MISSING]",
        batch="実行監視（cloud-batch-watchdog）",
        asof=verdict.check_date.isoformat(),
        details=lines,
        url=watchdog_run_url,
    )


def resolve_window_end(
    check_date: str, *, now: datetime, fire_time: time = SCHEDULED_FIRE_TIME
) -> datetime:
    """Return the instant the window closes at.

    Without ``--check-date`` the window closes now, so a delayed firing still sees
    the run it was waiting for. With one, it closes at the scheduled firing time on
    that date, which is what makes a past day reproducible from the same listing.
    """
    if not check_date:
        return now
    try:
        parsed = date.fromisoformat(check_date)
    except ValueError as exc:
        raise WatchdogInputError("check date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != check_date:
        raise WatchdogInputError("check date must use YYYY-MM-DD")
    return datetime.combine(parsed, fire_time)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_watchdog",
        description=("alert Discord #batch-runs when no cloud-daily-batch run succeeded recently"),
    )
    parser.add_argument("--runs-json", type=Path, required=True)
    parser.add_argument("--check-date", type=str, default="")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--read-run-results", action="store_true")
    parser.add_argument("--economic", action="store_true")
    parser.add_argument("--market", action="store_true")
    return parser


def _require_historical_window(payload: object, *, window_start: datetime) -> None:
    """A truncated first page cannot establish absence beyond its history."""
    if not isinstance(payload, Mapping):
        return  # The normal parser reports malformed listings.
    entries, total = payload.get("workflow_runs"), payload.get("total_count")
    if not isinstance(entries, list) or type(total) is not int or total <= len(entries):
        return
    runs = parse_runs(payload)
    if not runs or min(run.created_at for run in runs) > window_start:
        raise WatchdogInputError(
            "insufficient run history for the requested window; "
            "the truncated listing does not reach its start"
        )


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport = _urllib_transport,
    read_jobs: JobsReader = _read_attempt_jobs,
    read_quality_jobs: JobsReader = _read_quality_attempt_jobs,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ
    try:
        payload = json.loads(args.runs_json.read_text(encoding="utf-8"))
        history = payload.copy() if isinstance(payload, dict) else payload
        window_end = resolve_window_end(
            args.check_date,
            now=datetime.now(UTC),
            fire_time=ACQUISITION_WATCHDOG_FIRE_TIME
            if args.economic or args.market
            else SCHEDULED_FIRE_TIME,
        )
        if args.economic:
            if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
                raise WatchdogInputError("run listing must carry a workflow_runs array")
            # A limited successful rerun cannot stand in for all scheduled providers.
            payload["workflow_runs"] = [
                row
                for row in payload["workflow_runs"]
                if isinstance(row, dict)
                and str(row.get("display_title", "")).startswith("economic all all")
            ]
        scopes = {}
        if args.market:
            if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
                raise WatchdogInputError("market run listing is missing")
            retained = []
            for row in payload["workflow_runs"]:
                title = str(row.get("display_title", ""))
                for scope in (*MARKET_SCOPE_SCHEDULES, "all"):
                    if title.startswith("market-acquisition " + scope):
                        scopes[row["id"]] = scope
                        retained.append(row)
                        break
            payload["workflow_runs"] = retained
        runs = parse_runs(payload)
        if args.read_run_results:
            runs = resolve_run_results(
                runs,
                repository=env["GITHUB_REPOSITORY"],
                window_end=window_end,
                window_hours=args.window_hours,
                read_jobs=read_jobs,
                result_prefix="Economic result for "
                if args.economic
                else "Market source result for "
                if args.market
                else RESULT_STEP_PREFIX,
                repeated_source_results=args.economic or args.market,
            )
        verdict = evaluate(
            runs,
            window_end=window_end,
            window_hours=args.window_hours,
            target_date=acquisition_target_date(window_end)
            if args.economic or args.market
            else None,
            queued_delay_after=None if args.economic or args.market else DAILY_QUEUED_ALERT_AFTER,
        )
        if args.market:
            verdicts = [
                evaluate(
                    tuple(run for run in runs if scopes.get(run.run_id) in {scope, "all"}),
                    window_end=window_end,
                    window_hours=args.window_hours,
                    target_date=acquisition_target_date(
                        window_end, MARKET_SOURCE_SCHEDULES[schedule]
                    ),
                )
                for scope, schedule in MARKET_SCOPE_SCHEDULES.items()
            ]
            verdict = replace(
                verdict,
                state=STATE_MISSING
                if any(item.alerting for item in verdicts)
                else STATE_IN_FLIGHT
                if any(item.state == STATE_IN_FLIGHT for item in verdicts)
                else STATE_HEALTHY,
            )
        if args.check_date and verdict.alerting:
            # Use the unfiltered page: source selection is not history coverage.
            _require_historical_window(history, window_start=verdict.window_start)
        if (
            args.read_run_results
            and not (args.economic or args.market)
            and verdict.state == STATE_MISSING
        ):
            verdict = _resolve_missing_daily(verdict, env["GITHUB_REPOSITORY"], read_quality_jobs)
    except (OSError, json.JSONDecodeError, WatchdogInputError) as exc:
        print(f"error: cannot evaluate the batch window: {exc}", file=sys.stderr)
        return 1
    window = f"{verdict.window_start.isoformat()}..{verdict.window_end.isoformat()}"
    if not verdict.alerting:
        # Silence is the product on a healthy day: a second daily [OK] would train
        # the reader to skip the channel the alert has to reach.
        print(
            f"watchdog: {verdict.state} for {verdict.check_date.isoformat()}; "
            f"no gap to report inside {window}"
        )
        return 0
    message = render_alert(
        verdict,
        repository=env.get("GITHUB_REPOSITORY", "local/local"),
        watchdog_run_url=run_url(env),
        workflow="cloud-economic-sources"
        if args.economic
        else "cloud-market-sources"
        if args.market
        else WATCHED_WORKFLOW,
    )
    failure = deliver(
        env.get(WEBHOOK_ENV_VAR, ""), message, timeout=args.timeout, transport=transport
    )
    if failure is not None:
        print(f"error: watchdog alert failed: {failure}", file=sys.stderr)
        return 1
    reason = verdict.state if verdict.quality_run else "no successful run"
    print(
        f"watchdog: alert delivered; {reason} for {verdict.check_date.isoformat()} inside {window}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
