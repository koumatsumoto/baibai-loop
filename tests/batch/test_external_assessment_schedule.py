from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from tests.batch.test_external_assessment_daily import (
    FakeObjectStore,
    completed_pair,
    plan,
    seed_stage,
)
from tests.engine.test_external_assessment_daily_verification import build_master_lake
from tests.engine.test_external_assessment_pair_collection import Session
from tests.engine.test_external_assessment_sources import monex_html

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.jobs.external_assessment_cohorts import new_result
from baibai_batch.jobs.schedule import (
    EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME,
    external_assessment_target_date,
)
from baibai_batch.storage import external_assessment_staging as staging
from baibai_engine.external_assessment.acquisition import collection
from baibai_engine.storage.lake.keys import current_release_pointer_key

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


def test_scheduled_status_and_plan_reuse_the_original_id_before_any_new_lake_reads(
    tmp_path, monkeypatch
):
    p = replace(
        plan(),
        run_id="external-assessment-daily-2026-10-11",
        universe_binding=json.dumps({"master_asof": FRIDAY.isoformat()}),
    )
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    monkeypatch.setattr(
        daily,
        "external_assessment_calendar_master_asof",
        lambda *args, **kwargs: pytest.fail("refreeze"),
    )
    clock(monkeypatch, datetime(2026, 10, 12, 10, 13, tzinfo=UTC))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["--scheduled", "--run-created-at", CREATED.isoformat()]
    assert daily.main(["status", *args]) == 0
    assert outputs(output)["run_id"] == p.run_id
    assert outputs(output)["eligible"] == "true"
    assert daily.main(["plan", *args]) == 0
    assert outputs(output)["cohorts"] == "[0]"
    assert all(key.startswith(staging.prefix(p.run_id)) for _, key in store.events)


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


def test_before_close_scheduled_run_does_not_touch_store_or_provider(tmp_path, monkeypatch):
    clock(monkeypatch, datetime(2026, 10, 12, 6, 29, tzinfo=UTC))
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: pytest.fail("before-close R2"))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert daily.main(["status", "--scheduled", "--run-created-at", CREATED.isoformat()]) == 0
    assert outputs(output) == {"eligible": "false"}


@pytest.mark.parametrize(
    "args",
    [
        ["plan", "--scheduled"],
        ["acquire", "--scheduled", "--run-created-at", CREATED.isoformat()],
        ["plan", "--scheduled", "--run-created-at", CREATED.isoformat(), "--run-id", "override"],
        [
            "plan",
            "--scheduled",
            "--run-created-at",
            CREATED.isoformat(),
            "--master-asof",
            "2026-10-09",
        ],
        ["status", "--run-created-at", CREATED.isoformat(), "--run-id", "manual"],
    ],
)
def test_invalid_scheduled_or_manual_arguments_never_open_store(monkeypatch, args):
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: pytest.fail("invalid input R2"))
    assert daily.main(args) == 1


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

    monkeypatch.setattr(collection, "datetime", ReceiptDatetime)
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


def test_plan_matrix_omits_completed_cohorts_without_a_second_checkpoint_scan(
    tmp_path, monkeypatch
):
    p = plan(tickers=("6758", "7203"), cohort_size=1)
    store = FakeObjectStore()
    staging.save_new_plan(store, p)
    first = completed_pair(p, new_result(p, 0), "6758", "observed")
    seed_stage(store, p, first)
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["plan", "--run-id", p.run_id, "--master-asof", "2026-10-11"]
    assert daily.main(args) == 0
    assert outputs(output)["cohorts"] == "[1]"
    second = completed_pair(p, new_result(p, 1), "7203", "normal_null")
    seed_stage(store, p, second)
    before = {key: payload for key, (payload, _) in store.objects.items()}
    store.events.clear()
    store.puts.clear()
    assert daily.main(args) == 0
    assert outputs(output)["cohorts"] == "[]"
    assert sum(kind == "GET" and "/cohort-" in key for kind, key in store.events) == 2
    assert not store.puts
    assert {key: payload for key, (payload, _) in store.objects.items()} == before


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_workflow_reuses_plan_commands_and_resolved_id_without_a_second_entrypoint(tmp_path, event):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": "13 10 * * *"}]
    assert (
        EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME.hour,
        EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME.minute,
    ) == (10, 13)
    jobs = workflow["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "schedule" in jobs["notify"]["if"]
    assert "needs.plan.outputs.eligible != 'false'" in jobs["notify"]["if"]
    for job in ("acquire", "publish"):
        assert (
            jobs[job]["env"]["EXTERNAL_ASSESSMENT_BATCH_ID"] == "${{ needs.plan.outputs.batch_id }}"
        )
    steps = {step.get("id"): step for step in jobs["plan"]["steps"] if step.get("id")}
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
        "EXTERNAL_ASSESSMENT_BATCH_ID": "manual-fixed",
        "EXTERNAL_ASSESSMENT_MASTER_ASOF": "2026-10-09",
        "EXTERNAL_ASSESSMENT_OPERATION": "collect_and_publish",
        "RUNNER_TEMP": str(tmp_path),
        "EXTERNAL_ASSESSMENT_RUN_CREATED_AT": CREATED.isoformat(),
    }
    for step in ("status", "plan"):
        subprocess.run(["bash", "-euo", "pipefail", "-c", steps[step]["run"]], env=env, check=True)
    args = arguments.read_text().splitlines()
    if event == "schedule":
        assert args.count("--scheduled") == 2
        assert args.count(CREATED.isoformat()) == 2
        assert "manual-fixed" not in args
        assert "--master-asof" not in args
    else:
        assert args.count("manual-fixed") == 2
        assert "--scheduled" not in args
        assert "2026-10-09" in args
