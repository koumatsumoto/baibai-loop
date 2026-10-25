from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from tests.engine.test_external_assessment_daily_verification import build_master_lake
from tests.engine.test_external_assessment_pair_collection import Session
from tests.engine.test_external_assessment_sources import monex_html
from tests.jobs.test_external_assessment_daily import (
    FakeObjectStore,
    completed_pair,
    plan,
    seed_stage,
    wire_success,
)

from baibai_engine.data_collection.equity_web import http as source_http
from baibai_engine.data_collection.external_assessment import collection
from baibai_engine.storage.lake.keys import current_release_pointer_key
from baibai_jobs.jobs import external_assessment_daily as daily
from baibai_jobs.jobs import external_assessment_sources as sources
from baibai_jobs.jobs.external_assessment_cohorts import new_result
from baibai_jobs.jobs.schedule import (
    EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME,
    external_assessment_target_date,
)
from baibai_jobs.storage import external_assessment_staging as staging

CREATED = datetime(2026, 10, 11, 10, 13, tzinfo=UTC)
FRIDAY = date(2026, 10, 9)
CALENDAR = [("2026-10-09", 1), ("2026-10-10", 0), ("2026-10-11", 0), ("2026-10-12", 0)]
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def clock(monkeypatch, value):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.astimezone(tz)

    monkeypatch.setattr(daily, "datetime", FixedDatetime)
    monkeypatch.setattr(sources, "datetime", FixedDatetime)


@pytest.mark.parametrize(
    ("created", "now", "day", "eligible"),
    [
        (CREATED, CREATED, date(2026, 10, 11), True),
        (CREATED, datetime(2026, 10, 12, 10, 13, tzinfo=UTC), date(2026, 10, 11), True),
        (CREATED, datetime(2026, 10, 12, 6, 29, tzinfo=UTC), date(2026, 10, 11), False),
        (CREATED, datetime(2026, 10, 12, 6, 30, tzinfo=UTC), date(2026, 10, 11), True),
        (
            datetime(2026, 10, 12, 0, tzinfo=UTC),
            datetime(2026, 10, 12, 10, 13, tzinfo=UTC),
            date(2026, 10, 11),
            True,
        ),
    ],
)
def test_original_cron_day_survives_queue_delay_rerun_and_close_boundary(
    created, now, day, eligible
):
    assert external_assessment_target_date(created, now) == (day, eligible)


@pytest.mark.parametrize(
    ("created", "now"),
    [
        (CREATED.replace(tzinfo=None), CREATED),
        (CREATED, CREATED.replace(tzinfo=None)),
        (CREATED, datetime(2026, 10, 10, 10, 13, tzinfo=UTC)),
    ],
)
def test_invalid_run_clocks_fail_closed(created, now):
    with pytest.raises(ValueError, match="clock"):
        external_assessment_target_date(created, now)


@pytest.mark.parametrize("day", [date(2026, 10, 9), date(2026, 10, 11), date(2026, 10, 12)])
def test_new_daily_plan_reads_calendar_and_exact_master_only_and_resumes_its_release(
    tmp_path, monkeypatch, day
):
    store, release, _ = build_master_lake(tmp_path, monkeypatch, snapshot=FRIDAY, calendar=CALENDAR)
    run_id = f"external-assessment-daily-{day.isoformat()}"
    p = daily.initialize(
        store, run_id=run_id, master_asof=None, observation_date=day, mirror=tmp_path / "cache"
    )
    assert json.loads(p.universe_binding)["master_asof"] == FRIDAY.isoformat()
    assert json.loads(p.universe_binding)["release_manifest_sha256"] == release.manifest_sha256
    parquet_reads = [key for key in store.get_keys if key.endswith(".parquet")]
    assert parquet_reads
    assert all(
        any(name in key for name in ("jquants.master_snapshots/", "jquants.market_calendar/"))
        for key in parquet_reads
    )
    store.store_object(current_release_pointer_key(), b"unreadable newer pointer")
    store.get_keys.clear()
    store.head_keys.clear()
    assert (
        daily.initialize(store, run_id=run_id, master_asof=None, observation_date=day, mirror=None)
        == p
    )
    assert current_release_pointer_key() not in store.head_keys
    assert set(store.get_keys) == {staging.prefix(run_id) + "/plan.json"}


@pytest.mark.parametrize("fault", ["missing_day", "gap", "no_business", "missing_master"])
def test_unavailable_calendar_or_exact_master_creates_no_plan(tmp_path, monkeypatch, fault):
    calendar = list(CALENDAR)
    snapshot = FRIDAY
    if fault == "missing_day":
        calendar = calendar[:-1]
    elif fault == "gap":
        calendar = [row for row in calendar if row[0] != "2026-10-10"]
    elif fault == "no_business":
        calendar = [(day, 0) for day, _ in calendar]
    else:
        snapshot = date(2026, 10, 8)
    store, _, _ = build_master_lake(tmp_path, monkeypatch, snapshot=snapshot, calendar=calendar)
    store.put_keys.clear()
    with pytest.raises(ValueError, match=r"calendar|master"):
        daily.initialize(
            store,
            run_id="unavailable",
            master_asof=None,
            observation_date=date(2026, 10, 12),
            mirror=tmp_path / "cache",
        )
    assert not store.put_keys


def test_saved_plan_history_status_never_refreezes_its_universe(monkeypatch, capsys):
    p = replace(plan(), run_id="external-assessment-daily-monex-2026-10-11")
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    store.events.clear()
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    monkeypatch.setattr(daily, "initialize", lambda *args, **kwargs: pytest.fail("refreeze"))
    assert daily.main(["status", "--run-id", p.run_id]) == 0
    assert json.loads(capsys.readouterr().out) == {"plan_exists": True}
    assert all(key == staging.prefix(p.run_id) + "/plan.json" for _, key in store.events)


def test_new_year_rest_day_uses_previous_calendar_and_master_year(tmp_path, monkeypatch):
    basis = date(2025, 12, 30)
    calendar = [("2025-12-30", 1), ("2025-12-31", 0), ("2026-01-01", 0), ("2026-01-02", 0)]
    store, _, _ = build_master_lake(tmp_path, monkeypatch, snapshot=basis, calendar=calendar)
    p = daily.initialize(
        store,
        run_id="external-assessment-daily-2026-01-02",
        master_asof=None,
        observation_date=date(2026, 1, 2),
        mirror=tmp_path / "cache",
    )
    assert json.loads(p.universe_binding)["master_asof"] == basis.isoformat()
    master_reads = [
        key
        for key in store.get_keys
        if "jquants.master_snapshots/" in key and key.endswith(".parquet")
    ]
    assert master_reads
    assert all("/year=2025/" in key for key in master_reads)


def test_before_close_source_prepare_does_not_touch_store_or_provider(tmp_path, monkeypatch):
    clock(monkeypatch, datetime(2026, 10, 12, 6, 29, tzinfo=UTC))
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: pytest.fail("before-close R2"))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert (
        sources.main(["prepare", "--source", "monex", "--run-created-at", CREATED.isoformat()]) == 0
    )
    assert outputs(output)["eligible"] == "false"


@pytest.mark.parametrize("command", ["plan", "acquire", "publish", "publish-confirmed-partial"])
def test_old_write_commands_and_scheduled_identity_cannot_open_store(monkeypatch, command):
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: pytest.fail("old writer R2"))
    with pytest.raises(SystemExit) as error:
        daily.main(
            [command, "--scheduled", "--source", "monex", "--run-created-at", CREATED.isoformat()]
        )
    assert error.value.code == 2


def test_holiday_receipt_keeps_actual_day_and_same_plan_resume_never_repeats_http(monkeypatch):
    p = replace(
        plan(),
        run_id="external-assessment-daily-2026-10-11",
        universe_binding=json.dumps({"master_asof": FRIDAY.isoformat()}),
    )
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    client = Session([(200, monex_html())])
    monkeypatch.setattr(collection.requests, "Session", lambda: client)

    class ReceiptDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return CREATED.astimezone(tz)

    monkeypatch.setattr(source_http, "datetime", ReceiptDatetime)
    report = daily.acquire_cohort(store, p, 0)
    assert report["state_counts"]["success"] == 1
    saved = staging.read_stages(store, p)[0][0]
    receipt = saved.result.observations[0]
    assert receipt.fetched_at_utc == CREATED
    assert receipt.snapshot_date == CREATED.date()
    assert receipt.snapshot_date != FRIDAY
    before = store.objects[staging.stage_key(p, 0)][0]
    assert daily.acquire_cohort(store, p, 0)["stop_reason"] is None
    assert len(client.calls) == 1
    assert store.objects[staging.stage_key(p, 0)][0] == before


def test_source_runner_skips_completed_cohorts_and_complete_resume_has_no_http_or_put(monkeypatch):
    p = plan(tickers=("6758", "7203"), cohort_size=1)
    store = FakeObjectStore()
    seed_stage(store, p, completed_pair(p, new_result(p, 0), "6758", "observed"))
    calls = wire_success(monkeypatch, store, p)
    report = sources.acquire_source(store, p, source="monex")
    assert calls == ["7203"]
    assert report["status"] == "acquired"
    before = list(store.puts)
    repeated = sources.acquire_source(store, p, source="monex")
    assert repeated["provider_requests"] == 0
    assert calls == ["7203"]
    assert store.puts == before


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_workflow_pins_original_clock_and_passes_inputs_through_env(tmp_path, event):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": "13 10 * * *"}]
    assert (
        EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME.hour,
        EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME.minute,
    ) == (10, 13)
    job = workflow["jobs"]["plan"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
    assert "actions/runs/$GITHUB_RUN_ID" in steps["clock"]["run"]
    fake_uv = tmp_path / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$ARGUMENTS"\n')
    fake_uv.chmod(0o700)
    arguments = tmp_path / "arguments"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARGUMENTS": str(arguments),
        "GITHUB_EVENT_NAME": event,
        "SOURCE": "monex",
        "BATCH_ID": "manual-fixed",
        "MASTER_ASOF": "2026-10-09",
        "RUN_CREATED_AT": CREATED.isoformat(),
        "OPERATION": "collect",
    }
    subprocess.run(["bash", "-euo", "pipefail", "-c", steps["plan"]["run"]], env=env, check=True)
    args = arguments.read_text().splitlines()
    assert "external_assessment_sources" in " ".join(args)
    assert "monex" in args
    if event == "schedule":
        assert "--run-created-at" in args
        assert CREATED.isoformat() in args
        assert "manual-fixed" not in args
    else:
        assert "manual-fixed" in args
        assert "--master-asof" in args
        assert "--run-created-at" not in args
