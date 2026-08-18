"""Schedule-gap watchdog for the cloud daily batch.

``cloud-daily-batch`` reports its own outcome to Discord, so every failure mode it
can observe arrives as a push message. A run that never starts observes nothing:
no ``[OK]``, no ``[FAILED]``, no message at all. GitHub drops scheduled runs under
load, and a merge landing seconds before the cron replaces the schedule for that
day, so the gap is a recurring shape rather than a one-off. Absence of a message
is exactly what a human is worst at noticing, and the UI as-of and the workflow
history only answer when someone goes looking.

This adapter turns that silence into a message. It reads a ``gh api`` workflow-run
listing and posts to the same ``#batch-runs`` webhook only when the recent window
holds neither a successful run nor one still in flight. A healthy day sends nothing,
so a second daily ``[OK]`` never trains the reader to ignore the channel.

Dependency-free by design: only the Python standard library and no 3.13+ syntax,
so the watchdog job runs on the runner's system ``python3`` without ``uv``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path

from baibai_batch.observability.discord import (
    DEFAULT_TIMEOUT_SECONDS,
    WEBHOOK_ENV_VAR,
    Transport,
    _urllib_transport,
    deliver,
)
from baibai_batch.observability.summary import DELIVERY_DELIVERED, sanitize_one_line

WATCHED_WORKFLOW = "cloud-daily-batch"
MESSAGE_MAX_CHARS = 2000
RUNS_SHOWN = 5

# The watchdog's own cron, in UTC. `--check-date` evaluates the window a scheduled
# firing on that date would have used, which is what makes a past day reproducible.
SCHEDULED_FIRE_TIME = time(12, 0, tzinfo=UTC)

# How far back a successful run still counts. Both edges are set by the observed
# GitHub schedule delay (median about two hours):
#   - Long enough that the day's own batch (cron 07:43 UTC) stays inside even if the
#     watchdog itself fires hours late — a 20h window holds it until 03:43 UTC the
#     next day.
#   - Short enough that the *previous* day's batch falls outside, so yesterday's
#     success cannot mask today's gap. At the scheduled 12:00 UTC firing the window
#     opens at 16:00 UTC the day before, which yesterday's 07:43 UTC run clears by
#     more than eight hours of delay.
DEFAULT_WINDOW_HOURS = 20

_JST = timezone(timedelta(hours=9))

# `cloud-daily-batch`'s own cron, in UTC (16:43 JST). "Which day is this run for" is a
# question about that schedule, so it is anchored on the cron rather than on JST
# midnight: both this watchdog and the batch it watches are fired late by GitHub's
# schedule queue — about two hours at the median — and a firing that slips past
# midnight JST must keep asking about the same day rather than about a day whose batch
# is not due yet. A contract test pins this against the workflow.
BATCH_SCHEDULED_FIRE_TIME = time(7, 43, tzinfo=UTC)

# The `run-name` the batch declares. A scheduled run leaves the date empty; a recovery
# dispatch carries the `--asof` it was given, which is the only place the target day of
# a past-dated recovery is visible in the run listing at all.
RUN_NAME_PREFIX = "daily"


class WatchdogInputError(ValueError):
    """The run listing does not satisfy the shape this adapter reads."""


def batch_target_date(instant: datetime) -> date:
    """The JST day the batch firing at or before ``instant`` is responsible for."""

    fired = datetime.combine(instant.astimezone(UTC).date(), BATCH_SCHEDULED_FIRE_TIME)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()


def run_target_date(*, event: str, created_at: datetime, display_title: str) -> date | None:
    """Which day a run answers for, or ``None`` when the listing cannot say.

    A scheduled run always answers for its own day: the schedule event supplies no
    `asof`, so the batch reads today. A dispatch names its day in the run name, and one
    whose run name this cannot read is not evidence about any particular day — runs
    created before the workflow carried a run name are exactly that, and reading them
    as "today" is what let a past-dated recovery stand in for a missing batch.
    """

    if event == "schedule":
        return batch_target_date(created_at)
    if event != "workflow_dispatch":
        return None
    title = display_title.strip()
    if title == RUN_NAME_PREFIX:
        return batch_target_date(created_at)
    prefix = f"{RUN_NAME_PREFIX} "
    if not title.startswith(prefix):
        return None
    try:
        return date.fromisoformat(title[len(prefix) :].strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One run of the watched workflow, reduced to the fields the verdict uses."""

    run_number: int
    event: str
    status: str
    conclusion: str
    created_at: datetime
    target_date: date | None

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
            f"{sanitize_one_line(state)} {self.created_at.isoformat()} for {target}"
        )


STATE_HEALTHY = "healthy"
STATE_IN_FLIGHT = "in_flight"
STATE_MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the window holds, and therefore whether the batch is missing.

    ``in_flight`` is a separate answer from ``missing`` because a batch that started
    late is not a gap: it will report its own outcome when it finishes, including a
    ``[CANCELLED]`` if it hits the job timeout. Folding it into ``missing`` would send
    a false alarm every time the schedule queue runs long enough to push the batch past
    the watchdog, and a channel that cries wolf stops being read.
    """

    state: str
    check_date: date
    window_start: datetime
    window_end: datetime
    runs_in_window: tuple[WorkflowRun, ...]

    @property
    def alerting(self) -> bool:
        return self.state == STATE_MISSING


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
        runs.append(
            WorkflowRun(
                run_number=run_number,
                event=event,
                status=str(entry.get("status", "unknown")),
                conclusion=str(entry.get("conclusion") or ""),
                created_at=created_at,
                target_date=run_target_date(
                    event=event,
                    created_at=created_at,
                    display_title=str(entry.get("display_title") or ""),
                ),
            )
        )
    return tuple(runs)


def evaluate(runs: Sequence[WorkflowRun], *, window_end: datetime, window_hours: int) -> Verdict:
    """Decide whether the window holds a completed, successful run."""
    if window_hours <= 0:
        raise WatchdogInputError("window hours must be positive")
    window_start = window_end - timedelta(hours=window_hours)
    in_window = tuple(
        sorted(
            (run for run in runs if window_start <= run.created_at <= window_end),
            key=lambda run: run.created_at,
        )
    )
    check_date = batch_target_date(window_end)
    # A recovery dispatch for an earlier day says nothing about this one. Reading the
    # window as a set of runs rather than as a set of answered days is what let a
    # past-dated success — the shape that actually occurred on 2026-08-18 — cover a
    # missing batch. The same filter applies to a run still in flight for the same
    # reason: it will report an outcome about its own day, not about this one.
    answering = tuple(run for run in in_window if run.target_date == check_date)
    if any(run.succeeded for run in answering):
        state = STATE_HEALTHY
    elif any(run.in_flight for run in answering):
        state = STATE_IN_FLIGHT
    else:
        state = STATE_MISSING
    return Verdict(
        state=state,
        check_date=check_date,
        window_start=window_start,
        window_end=window_end,
        runs_in_window=in_window,
    )


def render_alert(verdict: Verdict, *, repository: str, watchdog_run_url: str) -> str:
    """Render the bounded alert message for a window with no successful run."""
    window_hours = int((verdict.window_end - verdict.window_start).total_seconds() // 3600)
    lines = [
        (
            f"[MISSING] no successful {WATCHED_WORKFLOW} run for "
            f"{verdict.check_date.isoformat()} in the last {window_hours}h"
        ),
        f"repo: {sanitize_one_line(repository)}",
        f"window: {verdict.window_start.isoformat()} .. {verdict.window_end.isoformat()}",
    ]
    if verdict.runs_in_window:
        lines.append("runs in window:")
        for run in verdict.runs_in_window[:RUNS_SHOWN]:
            lines.append(f"- {run.describe()}")
        if len(verdict.runs_in_window) > RUNS_SHOWN:
            lines.append(f"- +{len(verdict.runs_in_window) - RUNS_SHOWN} more")
    else:
        lines.append("runs in window: none (the schedule did not fire)")
    lines.append(f"watchdog: {sanitize_one_line(watchdog_run_url)}")
    message = "\n".join(lines)
    if len(message) > MESSAGE_MAX_CHARS:
        message = message[: MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    return message


def resolve_window_end(check_date: str, *, now: datetime) -> datetime:
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
    return datetime.combine(parsed, SCHEDULED_FIRE_TIME)


def _watchdog_run_url(env: Mapping[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "local/local")
    run_id = env.get("GITHUB_RUN_ID", "0")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
    return f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_watchdog",
        description=("alert Discord #batch-runs when no cloud-daily-batch run succeeded recently"),
    )
    parser.add_argument("--runs-json", type=Path, required=True)
    parser.add_argument("--check-date", type=str, default="")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ
    try:
        payload = json.loads(args.runs_json.read_text(encoding="utf-8"))
        runs = parse_runs(payload)
        window_end = resolve_window_end(args.check_date, now=datetime.now(UTC))
        verdict = evaluate(runs, window_end=window_end, window_hours=args.window_hours)
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
        watchdog_run_url=_watchdog_run_url(env),
    )
    delivery = deliver(
        env.get(WEBHOOK_ENV_VAR, ""), message, timeout=args.timeout, transport=transport
    )
    if delivery.status != DELIVERY_DELIVERED:
        print(f"error: watchdog alert failed: {delivery.detail}", file=sys.stderr)
        return 1
    print(
        f"watchdog: alert delivered; no successful run for "
        f"{verdict.check_date.isoformat()} inside {window}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
