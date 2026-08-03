from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tools.cloud.batch_watchdog import (
    DEFAULT_WINDOW_HOURS,
    WatchdogInputError,
    evaluate,
    main,
    parse_runs,
    render_alert,
    resolve_window_end,
)
from tools.cloud.validate_workflow_inputs import WorkflowInputError, validate_watchdog_input

ROOT = Path(__file__).resolve().parents[1]
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
) -> dict[str, object]:
    return {
        "run_number": number,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at.isoformat(),
    }


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

    assert verdict.healthy is True


def test_only_failed_runs_inside_the_window_is_not_healthy() -> None:
    runs = parse_runs(
        _listing(
            _run(number=7, created_at=FIRED_AT - timedelta(hours=2), conclusion="failure"),
            _run(
                number=8,
                created_at=FIRED_AT - timedelta(hours=1),
                status="in_progress",
                conclusion="",
            ),
        )
    )

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.healthy is False
    assert [run.run_number for run in verdict.runs_in_window] == [7, 8]


def test_yesterdays_successful_run_falls_outside_the_window() -> None:
    """The back edge is what stops yesterday's success from masking today's gap.

    `cloud-daily-batch` is scheduled at 08:23 UTC; at the watchdog's 12:00 UTC
    firing that run is 27.6 hours old, well beyond the window.
    """
    yesterday = FIRED_AT.replace(hour=8, minute=23) - timedelta(days=1)
    runs = parse_runs(_listing(_run(created_at=yesterday)))

    verdict = evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS)

    assert verdict.healthy is False
    assert verdict.runs_in_window == ()


def test_a_late_watchdog_firing_still_sees_the_days_batch() -> None:
    """The front edge absorbs the watchdog's own schedule delay.

    GitHub's schedule queue adds about two hours at the median; the window has to
    hold the day's 08:23 UTC batch even when the watchdog fires far later than due,
    otherwise the delay itself becomes the false alarm.
    """
    batch = FIRED_AT.replace(hour=8, minute=23)
    runs = parse_runs(_listing(_run(created_at=batch)))

    late = evaluate(
        runs,
        window_end=batch + timedelta(hours=DEFAULT_WINDOW_HOURS),
        window_hours=DEFAULT_WINDOW_HOURS,
    )

    assert late.healthy is True


def test_a_manual_recovery_dispatch_counts_as_the_days_run() -> None:
    runs = parse_runs(
        _listing(
            _run(created_at=FIRED_AT - timedelta(hours=3), conclusion="failure"),
            _run(number=2, created_at=FIRED_AT - timedelta(hours=1), event="workflow_dispatch"),
        )
    )

    assert evaluate(runs, window_end=FIRED_AT, window_hours=DEFAULT_WINDOW_HOURS).healthy is True


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
    payload = _listing(_run(created_at=datetime.now(UTC) - timedelta(hours=2)))

    exit_code, transport = _run_main(tmp_path, monkeypatch, payload)

    assert exit_code == 0
    assert transport.calls == []
    assert "healthy" in capsys.readouterr().out


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
