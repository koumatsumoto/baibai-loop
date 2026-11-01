from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from baibai_jobs.schedule import MARKET_SOURCE_SCHEDULES, market_source_target_date

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/cloud-market-sources.yml"
INPUT_SOURCES = [
    "jquants-market",
    "jquants-financial",
    "jpx-earnings",
    "jpx-regulations",
    "edinet-list",
]


def _workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def _planned_sources(output):
    sources = [entry["source"] for entry in json.loads(output["sources"])]
    if output["edinet_all"] == "true":
        jobs = _workflow()["jobs"]
        sources.append(jobs["edinet-list"]["with"]["source"])
        sources.extend(jobs["edinet-archives"]["strategy"]["matrix"]["source"])
    return sources


def _run_plan(
    tmp_path,
    *,
    created_at,
    schedule="",
    asof="",
    source="all",
    attempt="1",
    started_at="2026-10-29T16:00:00Z",
):
    steps = {step["id"]: step for step in _workflow()["jobs"]["plan"]["steps"] if "id" in step}
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['GH_CALL']).write_text(' '.join(sys.argv[1:]))\n"
        "fields = {'created_at': os.environ['ORIGINAL_CREATED_AT'], "
        "'run_started_at': os.environ['ACTUAL_STARTED_AT']}\n"
        "print(fields[sys.argv[-1].removeprefix('.')])\n"
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "ORIGINAL_CREATED_AT": created_at,
        "ACTUAL_STARTED_AT": started_at,
        "GH_CALL": str(tmp_path / "gh-call"),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": attempt,
        "GITHUB_OUTPUT": str(tmp_path / "clock-output"),
    }
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", steps["clock"]["run"]],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    clock_output = Path(env["GITHUB_OUTPUT"]).read_text()
    clock = dict(line.split("=", 1) for line in clock_output.splitlines())
    env.update(
        {
            "PYTHONPATH": str(ROOT / steps["plan"]["env"]["PYTHONPATH"]),
            "SOURCE": source,
            "ASOF": asof,
            "SCHEDULE": schedule,
            "RUN_CREATED_AT": clock["created_at"],
            "GITHUB_OUTPUT": str(tmp_path / "plan-output"),
        }
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", steps["plan"]["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output_path = Path(env["GITHUB_OUTPUT"])
    output = (
        dict(line.split("=", 1) for line in output_path.read_text().splitlines())
        if output_path.exists()
        else {}
    )
    assert (tmp_path / "gh-call").read_text() == (
        "api repos/owner/repo/actions/runs/123 --jq .created_at"
    )
    return result, output


def test_workflow_crons_match_supported_market_source_slots():
    crons = {slot["cron"] for slot in _workflow()["on"]["schedule"]}
    assert crons == set(MARKET_SOURCE_SCHEDULES)


@pytest.mark.parametrize(
    ("schedule", "created_at", "expected_day", "expected_sources"),
    [
        ("43 8 * * *", "2026-10-26T15:29:01Z", "2026-10-26", ["edinet-metrics"]),
        ("13 9 * * *", "2026-10-26T16:08:24Z", "2026-10-26", ["edinet-facts"]),
        ("43 7 * * *", "2026-10-24T00:05:00Z", "2026-10-23", INPUT_SOURCES),
        ("43 7 * * *", "2026-10-26T00:05:00Z", "2026-10-25", ["edinet-list"]),
    ],
)
def test_delayed_schedule_preserves_slot_date_and_weekday(
    tmp_path, schedule, created_at, expected_day, expected_sources
):
    result, output = _run_plan(tmp_path, created_at=created_at, schedule=schedule)
    assert result.returncode == 0, result.stderr
    assert output["asof"] == expected_day
    assert _planned_sources(output) == expected_sources
    assert output["edinet_all"] == "false"


@pytest.mark.parametrize("attempt", ["1", "2"])
def test_original_clock_survives_queue_wait_and_rerun(tmp_path, attempt):
    result, output = _run_plan(
        tmp_path,
        created_at="2026-10-26T15:29:01Z",
        started_at="2026-10-29T16:00:00Z",
        schedule="43 8 * * *",
        attempt=attempt,
    )
    assert result.returncode == 0, result.stderr
    assert output["asof"] == "2026-10-26"


@pytest.mark.parametrize("schedule", MARKET_SOURCE_SCHEDULES)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_each_cron_boundary_uses_latest_own_slot(schedule, offset):
    fire = datetime.combine(date(2026, 10, 26), MARKET_SOURCE_SCHEDULES[schedule])
    expected = date(2026, 10, 25) if offset < 0 else date(2026, 10, 26)
    target = market_source_target_date(fire + timedelta(seconds=offset), schedule=schedule)
    assert target == expected


@pytest.mark.parametrize("schedule", MARKET_SOURCE_SCHEDULES)
@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-10-26T14:59:59Z", date(2026, 10, 26)),
        ("2026-10-26T15:00:00Z", date(2026, 10, 26)),
        ("2026-12-31T23:59:59Z", date(2026, 12, 31)),
        ("2027-01-01T00:00:00Z", date(2026, 12, 31)),
    ],
)
def test_jst_and_utc_date_boundaries_keep_slot_date(schedule, created_at, expected):
    created = datetime.fromisoformat(created_at)
    assert market_source_target_date(created, schedule=schedule) == expected


@pytest.mark.parametrize(
    ("created_at", "asof", "source", "expected_day", "expected_sources"),
    [
        ("2026-10-26T14:59:59Z", "", "jpx-earnings", "2026-10-26", ["jpx-earnings"]),
        (
            "2026-10-26T15:00:00Z",
            "",
            "all",
            "2026-10-27",
            [*INPUT_SOURCES, "edinet-metrics", "edinet-facts"],
        ),
        (
            "2026-10-26T15:29:01Z",
            "2026-10-25",
            "edinet-facts",
            "2026-10-25",
            ["edinet-facts"],
        ),
    ],
)
def test_manual_explicit_and_empty_asof_remain_distinct(
    tmp_path, created_at, asof, source, expected_day, expected_sources
):
    result, output = _run_plan(tmp_path, created_at=created_at, asof=asof, source=source)
    assert result.returncode == 0, result.stderr
    assert output["asof"] == expected_day
    assert _planned_sources(output) == expected_sources
    assert output["edinet_all"] == str(source == "all").lower()


@pytest.mark.parametrize("attempt", ["1", "2"])
def test_manual_all_waits_for_list_only_at_the_archive_boundary(tmp_path, attempt):
    result, output = _run_plan(
        tmp_path,
        created_at="2026-10-29T12:00:00Z",
        started_at="2026-10-30T16:00:00Z",
        attempt=attempt,
    )
    assert result.returncode == 0, result.stderr
    assert output["asof"] == "2026-10-29"
    assert json.loads(output["sources"]) == [{"source": source} for source in INPUT_SOURCES[:-1]]
    jobs = _workflow()["jobs"]
    assert jobs["plan"]["outputs"]["edinet_all"] == "${{ steps.plan.outputs.edinet_all }}"
    assert jobs["source"]["needs"] == "plan"
    assert jobs["edinet-list"]["needs"] == "plan"
    archives = jobs["edinet-archives"]
    assert archives["needs"] == ["plan", "edinet-list"]
    assert archives["strategy"]["fail-fast"] == "false"
    assert archives["strategy"]["matrix"]["source"] == ["edinet-metrics", "edinet-facts"]
    for name in ("edinet-list", "edinet-archives"):
        job = jobs[name]
        # Keep GitHub's default success gate: failed list acquisition must not
        # launch archives against old inputs and present them as today's work.
        assert job["if"] == "${{ needs.plan.outputs.edinet_all == 'true' }}"
        assert job["uses"] == jobs["source"]["uses"]
        assert job["with"]["asof"] == "${{ needs.plan.outputs.asof }}"


@pytest.mark.parametrize("source", ["edinet-list", "edinet-metrics", "edinet-facts"])
def test_manual_single_edinet_source_remains_independent(tmp_path, source):
    result, output = _run_plan(
        tmp_path, created_at="2026-10-29T12:00:00Z", asof="2026-10-28", source=source
    )
    assert result.returncode == 0, result.stderr
    assert output["asof"] == "2026-10-28"
    assert output["edinet_all"] == "false"
    assert json.loads(output["sources"]) == [{"source": source}]


@pytest.mark.parametrize("asof", ["", "2026-10-26"])
def test_unknown_cron_is_rejected_even_with_explicit_asof(tmp_path, asof):
    result, output = _run_plan(
        tmp_path, created_at="2026-10-26T15:29:01Z", schedule="0 8 * * *", asof=asof
    )
    assert result.returncode != 0
    assert "unknown market source schedule" in result.stderr
    assert output == {}
