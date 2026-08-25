from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from baibai_batch.jobs.watchdog import (
    BATCH_SCHEDULED_FIRE_TIME,
    DEFAULT_WINDOW_HOURS,
    RUN_NAME_PREFIX,
    STATE_HEALTHY,
    STATE_IN_FLIGHT,
    STATE_MISSING,
    WatchdogInputError,
    batch_target_date,
    evaluate,
    main,
    parse_runs,
    render_alert,
    resolve_window_end,
    unattended,
)
from baibai_batch.validation.workflow_inputs import WorkflowInputError, validate_watchdog_input

ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_WORKFLOW = ROOT / ".github/workflows/cloud-batch-watchdog.yml"

# The instant a scheduled watchdog firing on 2026-08-03 evaluates.
FIRED_AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
WEBHOOK = "https://discord.com/api/webhooks/111/token-value"


class FakeTransport:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> int:
        self.calls.append((url, body, timeout))
        return self.status


def _run(
    *,
    number: int = 1,
    created_at: datetime,
    status: str = "completed",
    conclusion: str = "success",
    event: str = "schedule",
    display_title: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "run_number": number,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at.isoformat(),
    }
    if display_title is not None:
        entry["display_title"] = display_title
    return entry


def _listing(*runs: dict[str, object]) -> dict[str, object]:
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def _write_listing(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _delivered_message(transport: FakeTransport) -> str:
    assert len(transport.calls) == 1
    body = json.loads(transport.calls[0][1].decode("utf-8"))
    content = body["content"]
    assert isinstance(content, str)
    return content


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    *,
    argv: list[str] | None = None,
    webhook: str = WEBHOOK,
    transport: FakeTransport | None = None,
) -> tuple[int, FakeTransport]:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", webhook)
    monkeypatch.setenv("GITHUB_REPOSITORY", "koumatsumoto/baibai-loop")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    used = transport if transport is not None else FakeTransport()
    runs_json = _write_listing(tmp_path, payload)
    exit_code = main(
        ["--runs-json", str(runs_json), *(argv or [])],
        transport=used,
    )
    return exit_code, used


# --- verdict ---------------------------------------------------------------


def test_a_successful_run_inside_the_window_is_healthy() -> None:
    runs = parse_runs(_listing(_run(created_at=FIRED_AT - timedelta(hours=2))))

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.state == STATE_HEALTHY
    assert verdict.alerting is False


def test_only_failed_runs_inside_the_window_is_a_gap() -> None:
    runs = parse_runs(
        _listing(
            _run(number=7, created_at=FIRED_AT - timedelta(hours=2), conclusion="failure"),
            _run(number=8, created_at=FIRED_AT - timedelta(hours=1), conclusion="cancelled"),
        )
    )

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.state == STATE_MISSING
    assert [run.run_number for run in verdict.runs_in_window] == [7, 8]


def test_a_batch_still_running_when_the_watchdog_fires_is_not_a_gap() -> None:
    """A late start is not a missing run, and alerting on it would be the false alarm.

    The schedule queue can push the 07:43 UTC batch past the watchdog's 12:00 UTC
    firing. That run still reports its own outcome when it finishes — including
    `[CANCELLED]` if it hits the job timeout — so the watchdog has nothing to add.
    """
    runs = parse_runs(
        _listing(
            _run(
                number=9,
                created_at=FIRED_AT - timedelta(minutes=20),
                status="in_progress",
                conclusion="",
            )
        )
    )

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.state == STATE_IN_FLIGHT
    assert verdict.alerting is False


def test_a_queued_run_is_also_treated_as_in_flight() -> None:
    runs = parse_runs(
        _listing(
            _run(
                number=10,
                created_at=FIRED_AT - timedelta(minutes=5),
                status="queued",
                conclusion="",
            )
        )
    )

    assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).alerting is False


def test_yesterdays_successful_run_falls_outside_the_window() -> None:
    """The back edge is what stops yesterday's success from masking today's gap.

    `cloud-daily-batch` is scheduled at 07:43 UTC; at the watchdog's 12:00 UTC
    firing that run is more than 28 hours old, well beyond the window.
    """
    yesterday = FIRED_AT.replace(hour=7, minute=43) - timedelta(days=1)
    runs = parse_runs(_listing(_run(created_at=yesterday)))

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.state == STATE_MISSING
    assert verdict.runs_in_window == ()


def test_a_late_watchdog_firing_still_sees_the_days_batch() -> None:
    """The front edge absorbs the watchdog's own schedule delay.

    GitHub's schedule queue adds about two hours at the median; the window has to
    hold the day's 07:43 UTC batch even when the watchdog fires far later than due,
    otherwise the delay itself becomes the false alarm.
    """
    batch = FIRED_AT.replace(hour=7, minute=43)
    runs = parse_runs(_listing(_run(created_at=batch)))

    late = evaluate(
        runs,
        window_end=batch + timedelta(hours=DEFAULT_WINDOW_HOURS),
        window_hours=DEFAULT_WINDOW_HOURS,
    )

    assert late.state == STATE_HEALTHY


def test_a_manual_recovery_dispatch_for_today_counts_as_the_days_run() -> None:
    runs = parse_runs(
        _listing(
            _run(created_at=FIRED_AT - timedelta(hours=3), conclusion="failure"),
            _run(
                number=2,
                created_at=FIRED_AT - timedelta(hours=1),
                event="workflow_dispatch",
                display_title="daily",
            ),
        )
    )

    assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).alerting is False


def test_a_recovery_dispatch_for_an_earlier_day_does_not_cover_this_one() -> None:
    """The shape that actually occurred: 2026-08-17's batch failed three times and the
    recovery ran the next morning. Had 08-18's own schedule also gone missing, a window
    holding that success would have read healthy."""

    runs = parse_runs(
        _listing(
            _run(
                created_at=FIRED_AT - timedelta(hours=4),
                event="workflow_dispatch",
                display_title="daily 2026-08-02",
            ),
        )
    )

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.check_date == date(2026, 8, 3)
    assert verdict.state == STATE_MISSING
    assert verdict.runs_in_window[0].target_date == date(2026, 8, 2)


def test_a_recovery_dispatch_for_an_earlier_day_is_not_in_flight_cover_either() -> None:
    runs = parse_runs(
        _listing(
            _run(
                created_at=FIRED_AT - timedelta(minutes=20),
                status="in_progress",
                conclusion="",
                event="workflow_dispatch",
                display_title="daily 2026-08-02",
            ),
        )
    )

    assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).state == (
        STATE_MISSING
    )


def test_runs_created_before_the_workflow_carried_a_run_name_still_count_when_scheduled() -> None:
    """The transition costs nothing on the schedule side: a schedule event supplies no
    `asof`, so a scheduled run answers for its own day whatever its title says."""

    runs = parse_runs(
        _listing(
            _run(created_at=FIRED_AT - timedelta(hours=3), display_title="Merge pull request #1"),
        )
    )

    assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).alerting is False


def test_a_dispatch_whose_run_name_cannot_be_read_answers_for_no_day() -> None:
    for title in (None, "cloud-daily-batch", "daily not-a-date", "daily 2026-13-40"):
        runs = parse_runs(
            _listing(
                _run(
                    created_at=FIRED_AT - timedelta(hours=1),
                    event="workflow_dispatch",
                    display_title=title,
                ),
            )
        )

        assert runs[0].target_date is None, title
        assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).state == (
            STATE_MISSING
        )


def test_the_day_a_run_answers_for_is_anchored_on_the_batch_cron_not_on_midnight() -> None:
    """Both this watchdog and the batch are fired late by GitHub's queue — about two
    hours at the median. A firing that slips past midnight JST must keep asking about
    the same day rather than about one whose batch is not due yet."""

    # 15:30 UTC on 2026-08-03 is 00:30 JST on 08-04, past JST midnight.
    late = datetime(2026, 8, 3, 15, 30, tzinfo=UTC)

    assert batch_target_date(late) == date(2026, 8, 3)
    # 07:00 UTC is before the batch's own 07:43 UTC cron, so the day in question is
    # still the previous one.
    assert batch_target_date(datetime(2026, 8, 3, 7, 0, tzinfo=UTC)) == date(2026, 8, 2)
    assert batch_target_date(datetime(2026, 8, 3, 7, 43, tzinfo=UTC)) == date(2026, 8, 3)


def test_the_batch_cron_this_anchors_on_is_the_one_the_workflow_declares() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/cloud-daily-batch.yml").read_text(encoding="utf-8")
    )
    crons = [entry["cron"] for entry in workflow[True]["schedule"]]

    assert len(crons) == 1
    minute, hour, *_rest = crons[0].split()
    assert (int(hour), int(minute)) == (
        BATCH_SCHEDULED_FIRE_TIME.hour,
        BATCH_SCHEDULED_FIRE_TIME.minute,
    )


def test_the_batch_run_name_is_the_one_the_watchdog_parses() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/cloud-daily-batch.yml").read_text(encoding="utf-8")
    )
    run_name = workflow["run-name"]

    assert run_name.startswith(f"{RUN_NAME_PREFIX} ")
    assert "inputs.asof" in run_name


# --- input handling (fail closed) ------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"total_count": 0},
        {"workflow_runs": {"run_number": 1}},
        {"workflow_runs": ["not-an-object"]},
        {"workflow_runs": [{"event": "schedule", "created_at": "2026-08-03T09:00:00Z"}]},
        {"workflow_runs": [{"run_number": 1, "created_at": "yesterday"}]},
        {"workflow_runs": [{"run_number": 1}]},
    ],
)
def test_a_listing_that_is_not_the_expected_shape_is_rejected(payload: object) -> None:
    """A parse that degraded to "no runs" would make every API change a false alarm."""
    with pytest.raises(WatchdogInputError):
        parse_runs(payload)


def test_a_run_timestamp_without_a_zone_is_read_as_utc() -> None:
    runs = parse_runs({"workflow_runs": [{"run_number": 1, "created_at": "2026-08-03T10:00:00"}]})

    assert runs[0].created_at == datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def test_an_empty_check_date_closes_the_window_at_the_current_instant() -> None:
    now = datetime(2026, 8, 3, 14, 31, tzinfo=UTC)

    assert resolve_window_end("", now=now) == now


def test_a_check_date_closes_the_window_at_the_scheduled_firing() -> None:
    assert resolve_window_end("2026-08-01", now=FIRED_AT) == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-8-1", "2026/08/01", "01-08-2026", "today", "2026-13-01"])
def test_a_check_date_that_is_not_an_exact_iso_date_is_rejected(value: str) -> None:
    with pytest.raises(WatchdogInputError):
        resolve_window_end(value, now=FIRED_AT)


@pytest.mark.parametrize("value", ["2026-8-1", "2026/08/01", "bogus"])
def test_the_dispatch_validator_rejects_a_malformed_check_date(value: str) -> None:
    with pytest.raises(WorkflowInputError):
        validate_watchdog_input(check_date=value)


def test_the_dispatch_validator_accepts_the_scheduled_empty_value() -> None:
    validate_watchdog_input(check_date="")
    validate_watchdog_input(check_date="2026-08-01")


def test_a_non_positive_window_is_rejected() -> None:
    with pytest.raises(WatchdogInputError):
        evaluate((), window_end=FIRED_AT, window_hours=0)


# --- message ---------------------------------------------------------------


def test_the_alert_names_the_empty_window_when_the_schedule_never_fired() -> None:
    verdict = evaluate((), window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    message = render_alert(verdict, repository="owner/repo", watchdog_run_url="https://run")

    assert "[MISSING]" in message
    assert "runs in window: none" in message
    assert "https://run" in message


def test_the_alert_lists_the_runs_that_did_happen_but_did_not_succeed() -> None:
    runs = parse_runs(
        _listing(_run(number=91, created_at=FIRED_AT - timedelta(hours=2), conclusion="failure"))
    )
    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    message = render_alert(verdict, repository="owner/repo", watchdog_run_url="https://run")

    assert "#91 schedule failure" in message


# --- CLI -------------------------------------------------------------------


def test_main_sends_nothing_when_the_window_holds_a_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both instants come from `--check-date`, because the geometry needs both.

    An empty check date closes the window at the wall clock, and the day a run answers
    for is anchored on the batch cron — so a run placed at an offset from `now` lands on
    the wrong side of that anchor for part of every day. Pinning the firing keeps the one
    realistic shape (batch at 07:43 UTC, watchdog at 12:00 UTC) true at every hour.
    """
    payload = _listing(_run(created_at=FIRED_AT - timedelta(hours=2)))

    exit_code, transport = _run_main(
        tmp_path, monkeypatch, payload, argv=["--check-date", "2026-08-03"]
    )

    assert exit_code == 0
    assert transport.calls == []
    assert "watchdog: healthy" in capsys.readouterr().out


def test_main_sends_nothing_while_the_days_batch_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _listing(
        _run(
            created_at=FIRED_AT - timedelta(minutes=15),
            status="in_progress",
            conclusion="",
        )
    )

    exit_code, transport = _run_main(
        tmp_path, monkeypatch, payload, argv=["--check-date", "2026-08-03"]
    )

    assert exit_code == 0
    assert transport.calls == []
    assert "watchdog: in_flight" in capsys.readouterr().out


def test_main_alerts_once_when_no_run_succeeded_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _listing(_run(created_at=datetime(2026, 7, 30, 9, 0, tzinfo=UTC)))

    exit_code, transport = _run_main(
        tmp_path, monkeypatch, payload, argv=["--check-date", "2026-08-01"]
    )

    assert exit_code == 0
    assert "[MISSING]" in _delivered_message(transport)
    assert "alert delivered" in capsys.readouterr().out


def test_main_fails_when_the_alert_cannot_be_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, transport = _run_main(
        tmp_path, monkeypatch, _listing(), webhook="https://example.invalid/hook"
    )

    assert exit_code == 1
    assert transport.calls == []


def test_main_fails_without_alerting_when_the_listing_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, transport = _run_main(tmp_path, monkeypatch, {"total_count": 0})

    assert exit_code == 1
    assert transport.calls == []


def test_main_fails_without_alerting_when_the_listing_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    transport = FakeTransport()

    exit_code = main(["--runs-json", str(tmp_path / "absent.json")], transport=transport)

    assert exit_code == 1
    assert transport.calls == []


# --- workflow contract -----------------------------------------------------


def _watchdog_job() -> dict[str, object]:
    workflow = yaml.load(WATCHDOG_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["watchdog"]
    assert isinstance(job, dict)
    return job


def test_the_watchdog_reads_run_history_without_write_permission() -> None:
    workflow = yaml.load(WATCHDOG_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)

    assert workflow["permissions"] == {"contents": "read", "actions": "read"}


def test_the_watchdog_stays_out_of_the_publish_queue_it_watches() -> None:
    """Sharing `cloud-publish` would queue the watchdog behind the run it reports on."""
    workflow = yaml.load(WATCHDOG_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)

    assert workflow["concurrency"]["group"] == "cloud-batch-watchdog"


def test_the_watchdog_installs_nothing_so_an_alert_never_waits_on_a_toolchain() -> None:
    steps = _watchdog_job()["steps"]
    assert isinstance(steps, list)

    uses = [str(step.get("uses", "")) for step in steps if isinstance(step, dict)]

    assert [reference for reference in uses if reference] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    ]


def test_the_watchdog_fires_on_weekdays_after_the_batch_is_due() -> None:
    workflow = yaml.load(WATCHDOG_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    daily = yaml.load(
        (ROOT / ".github/workflows/cloud-daily-batch.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(daily, dict)

    watchdog_cron = workflow["on"]["schedule"][0]["cron"]
    batch_cron = daily["on"]["schedule"][0]["cron"]
    watchdog_minute, watchdog_hour, _, _, watchdog_days = watchdog_cron.split()
    batch_minute, batch_hour, _, _, batch_days = batch_cron.split()

    assert watchdog_days == batch_days
    assert (int(watchdog_hour), int(watchdog_minute)) > (int(batch_hour), int(batch_minute))


def _day(offset: int) -> datetime:
    """A scheduled firing `offset` days before the reference date, in the batch window."""

    return datetime(2026, 8, 3, 8, 0, tzinfo=UTC) - timedelta(days=offset)


def test_unattended_counts_answered_days_not_runs() -> None:
    """A day retried three times is one answered day, not three."""

    runs = parse_runs(
        _listing(
            _run(created_at=_day(0), conclusion="failure"),
            _run(created_at=_day(0), conclusion="failure"),
            _run(created_at=_day(0), conclusion="success"),
            _run(created_at=_day(1), conclusion="failure"),
            _run(created_at=_day(2), conclusion="success"),
        )
    )

    rate = unattended(runs)

    assert rate is not None
    assert (rate.answered, rate.total) == (2, 3)
    assert rate.describe().startswith("2/3 days (67%)")


def test_unattended_is_absent_when_the_listing_dates_nothing() -> None:
    assert unattended(()) is None


def test_the_alert_carries_the_completion_it_was_given() -> None:
    """The aggregate cost of every guard, in front of whoever is reading a failure."""

    runs = parse_runs(
        _listing(
            _run(created_at=_day(0), conclusion="failure"),
            _run(created_at=_day(1), conclusion="success"),
        )
    )
    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    message = render_alert(
        verdict,
        repository="owner/repo",
        watchdog_run_url="https://example.invalid/run",
        rate=unattended(runs),
    )

    assert "unattended: 1/2 days (50%)" in message


def test_the_alert_omits_completion_when_there_is_none() -> None:
    verdict = evaluate((), window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    message = render_alert(
        verdict, repository="owner/repo", watchdog_run_url="https://example.invalid/run"
    )

    assert "unattended:" not in message


def test_a_day_rescued_by_hand_is_not_an_unattended_success() -> None:
    """The cost being measured is the intervention, so a rescued day must still count."""

    runs = parse_runs(
        _listing(
            _run(created_at=_day(0), conclusion="failure"),
            _run(created_at=_day(0), conclusion="success", event="workflow_dispatch"),
            _run(created_at=_day(1), conclusion="success"),
        )
    )

    rate = unattended(runs)

    assert rate is not None
    assert (rate.answered, rate.total) == (1, 2)
