"""Each source acquires and freezes a handoff; only common integration publishes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from tests.engine.test_external_assessment_sources import official_html, rakuten_html
from tests.engine.test_external_assessment_host_rate import Clock, Session
from tests.jobs.external_assessment_publication_fixtures import (
    integrate_ready_plans,
)
from tests.jobs.external_assessment_publication_fixtures import (
    independent_plans as independent_plans,
)
from tests.jobs.external_assessment_publication_fixtures import publication as publication
from tests.jobs.test_external_assessment_daily import FakeObjectStore, completed_pair, plan

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.sources as sources
import baibai_jobs.acquisition.external_assessments.staging as staging
from baibai_jobs.acquisition.external_assessments.cohorts import new_result, plan_bytes


def test_source_ids_are_disjoint_and_original_run_date_is_preserved(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 12, 10, 13, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(sources, "datetime", Clock)
    selected, day, eligible = sources.selection(
        source="all", batch_id=None, created_at=datetime(2026, 10, 11, 10, 13, tzinfo=UTC)
    )
    assert eligible
    assert day.isoformat() == "2026-10-11"
    assert len({item["run_id"] for item in selected}) == 4
    assert all(
        item["run_id"] == f"external-assessment-daily-{item['source']}-2026-10-11"
        for item in selected
    )
    one, _, _ = sources.selection(source="monex", batch_id="manual", created_at=None)
    other, _, _ = sources.selection(source="minkabu", batch_id="manual", created_at=None)
    assert one[0]["run_id"] != other[0]["run_id"]
    assert sources.read_selection(json.dumps(one)) == one
    scheduled = selected[0]["run_id"]
    resumed, _, _ = sources.selection(source="monex", batch_id=scheduled, created_at=None)
    assert resumed == selected[:1]
    for wrong_source in ("all", "minkabu"):
        with pytest.raises(ValueError, match="one exact source"):
            sources.selection(source=wrong_source, batch_id=scheduled, created_at=None)
    with pytest.raises(ValueError, match="namespaced"):
        sources.read_selection('[{"source":"minkabu","run_id":"monex-manual"}]')


def test_manual_scheduled_resume_requires_existing_exact_plan(monkeypatch, tmp_path):
    recorded = []
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: object())

    def initialize(*args, **kwargs):
        recorded.append(kwargs)
        raise ValueError("synthetic missing saved plan")

    monkeypatch.setattr(daily, "initialize", initialize)
    assert (
        sources.main(
            [
                "acquire",
                "--source",
                "minkabu",
                "--run-id",
                "external-assessment-daily-minkabu-2026-10-22",
                "--master-asof",
                "2026-10-22",
                "--directory",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert recorded[0]["require_existing"] is True
    assert recorded[0]["source"] == "minkabu"


def test_old_multi_source_plan_is_never_rebound():
    old = plan(sources=("monex", "rakuten"))
    with pytest.raises(ValueError, match="old multi-source"):
        sources.require_source(old, "monex")


@pytest.mark.parametrize("elapsed", [10, 1101])
def test_one_source_shares_session_limiter_and_remaining_budget_across_cohorts(
    monkeypatch, elapsed
):
    p = plan(tickers=("6758", "7203"), cohort_size=1)
    store = FakeObjectStore()
    saved = {}
    timer = [0.0]
    calls = []
    monkeypatch.setattr(sources.time, "monotonic", lambda: timer[0])
    monkeypatch.setattr(sources, "read_stages", lambda *_: saved)

    def acquire(_store, own, index, **kwargs):
        calls.append(kwargs)
        journal = completed_pair(
            own, new_result(own, index), own.cohorts[index].tickers[0], "observed"
        )
        saved[index] = (staging.CohortStage(journal), "synthetic")
        timer[0] += elapsed
        return {"provider_requests": 1, "cohort_complete": True, "stop_reason": None}

    monkeypatch.setattr(daily, "acquire_cohort", acquire)
    report = sources.acquire_source(store, p, source="monex", budget=2000)
    assert calls[0]["soft_budget_seconds"] == 2000
    if elapsed == 10:
        assert len(calls) == 2
        assert calls[1]["soft_budget_seconds"] == 1990
        assert calls[0]["session"] is calls[1]["session"]
        assert calls[0]["rate_limiter"] is calls[1]["rate_limiter"]
        assert report["status"] == "acquired"
    else:
        assert len(calls) == 1
        assert report["status"] == "incomplete"
        assert report["stop_reason"] == "soft_budget"
        assert report["state_counts"]["unattempted"] == 1


def test_one_source_403_stops_its_unattempted_pairs_while_other_source_acquires(
    monkeypatch,
):
    timer = Clock()
    monkeypatch.setattr(sources.time, "monotonic", timer.monotonic)
    monkeypatch.setattr(sources.time, "sleep", timer.sleep)
    denied = Session(timer, [(403, "synthetic denial", {})])
    available = Session(timer, [(200, official_html(), {}), (200, rakuten_html(), {})])
    clients = iter((denied, available))
    monkeypatch.setattr(sources.requests, "Session", lambda: next(clients))
    store = FakeObjectStore()
    failed = sources.acquire_source(
        store,
        replace(plan(tickers=("7203", "9432")), run_id="monex-blocked-acquire"),
        source="monex",
    )
    successful = sources.acquire_source(
        store, replace(plan(sources=("rakuten",)), run_id="rakuten-ready-acquire"), source="rakuten"
    )
    assert failed["status"] == "blocked"
    assert failed["state_counts"]["failure"] == 1
    assert failed["state_counts"]["unattempted"] == 1
    assert len(denied.calls) == 1
    assert successful["status"] == "acquired"
    assert successful["state_counts"]["success"] == 1
    assert len(available.calls) == 2
    assert available.calls[1][0] - available.calls[0][0] >= 0.5


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("old_binding", [False, True])
def test_sealed_source_inspection_and_acquire_read_no_stage_http_lease_or_db(
    independent_plans, monkeypatch, tmp_path, partial, old_binding
):
    store, selected, handle, database, mirror, directory = independent_plans
    selected = selected[:1]
    one = (store, selected, handle, database, mirror, directory)
    p = staging.read_plan(store, selected[0]["run_id"])
    if old_binding:
        checkpoint, _ = staging.read_stage(store, p, 0)
        p = replace(p, collector_binding="previous-collector")
        rebound = replace(checkpoint, result=replace(checkpoint.result, plan_sha256=p.plan_sha256))
        store.store_object(staging.prefix(p.run_id) + "/plan.json", plan_bytes(p))
        store.store_object(staging.stage_key(p, 0), staging.stage_bytes(p, rebound))
    integrate_ready_plans(one, [p], "sealed-source", confirmed_partial=partial)
    terminal_key = staging.prefix(p.run_id) + "/published.json"
    sealed_bytes = store.values[terminal_key].body
    store.put_keys.clear()
    store.get_keys.clear()
    store.head_keys.clear()

    def forbid(*args, **kwargs):
        pytest.fail("sealed source cannot touch stage/HTTP/lease/DB/current release")

    for name in ("read_stages", "prepare_plan"):
        monkeypatch.setattr(sources, name, forbid)
    for name in (
        "acquire_cohort",
        "require_binding",
        "_serving_pointer",
        "external_assessment_collect_pair",
    ):
        monkeypatch.setattr(daily, name, forbid)
    monkeypatch.setattr(sources.requests, "Session", forbid)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: store)
    expected = "already_partially_published" if partial else "already_published"
    ready, reports = sources.inspect_ready(store, selected)
    assert ready == []
    assert reports["monex"]["status"] == expected
    assert reports["monex"]["readback"] == "previously_verified"
    assert (
        sources.main(
            [
                "acquire",
                "--source",
                "monex",
                "--run-id",
                p.run_id,
                "--master-asof",
                "2026-10-09",
                "--directory",
                str(tmp_path / "unused-cache"),
            ]
        )
        == 0
    )
    outputs = tmp_path / "outputs"
    result_path = tmp_path / "source-results.json"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    assert (
        sources.main(
            [
                "inspect",
                "--selection-json",
                json.dumps(selected),
                "--report-output",
                str(result_path),
            ]
        )
        == 0
    )
    assert outputs.read_text() == "ready=false\n"
    assert json.loads(result_path.read_text())["sources"][0]["status"] == expected
    assert store.values[terminal_key].body == sealed_bytes
    assert store.put_keys == []
    assert set(store.get_keys) <= {staging.prefix(p.run_id) + "/plan.json", terminal_key}
    assert not (tmp_path / "unused-cache").exists()


def test_workflow_stages_each_source_without_waiting_for_its_siblings():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"
    parent = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert parent["on"]["workflow_dispatch"]["inputs"]["operation"]["options"] == [
        "collect",
        "stage_confirmed_partial",
    ]
    assert "publish" not in parent["jobs"]
    source = parent["jobs"]["source"]
    assert source["needs"] == "plan"
    assert source["strategy"]["fail-fast"] == "false"
    assert (
        source["strategy"]["matrix"]["include"] == "${{ fromJSON(needs.plan.outputs.source_runs) }}"
    )
    assert source["uses"] == "./.github/workflows/cloud-external-assessment-source.yml"
    assert source["with"]["run_id"] == "${{ matrix.run_id }}"
    child = yaml.load(
        path.with_name("cloud-external-assessment-source.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert child["concurrency"]["group"] == "external-assessment-acquire-${{ inputs.source }}"
    acquire = child["jobs"]["acquire"]
    assert "inputs.operation == 'collect'" in acquire["if"]
    assert "cloud-publish" not in str(acquire)
    assert "publish" not in child["jobs"]
    handoff = child["jobs"]["handoff"]
    assert handoff["needs"] == "acquire"
    assert "always()" in handoff["if"]
    assert "needs.acquire.result" not in handoff["if"]
    ready = next(step for step in handoff["steps"] if step.get("id") == "ready")
    assert "acquisition.external_assessments.sources ready" in ready["run"]
    assert '--selection-json "$SOURCE_RUNS"' in ready["run"]
    assert '--master-asof "$MASTER_ASOF"' in ready["run"]
    assert "--confirmed-partial" in ready["run"]
    for forbidden in (
        "cloud-publish",
        "publication_lease",
        "pull-market",
        "hydrate-market",
        "publish-lake",
    ):
        assert forbidden not in str(child)


def test_source_cli_has_no_standalone_publication_or_lease_inputs(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("retired publication command constructed a remote store")

    monkeypatch.setattr(sources, "Boto3R2Store", forbidden)
    with pytest.raises(SystemExit) as error:
        sources.main(["publish"])
    assert error.value.code == 2


def test_ready_inspection_is_scoped_and_partial_confirmation_is_single_source(independent_plans):
    store, selected, _, database, _, _ = independent_plans
    before = database.read_bytes()
    ready, reports = sources.inspect_ready(store, selected[:1])
    assert len(ready) == 1
    assert ready[0].plan.sources == ("monex",)
    assert set(reports) == {"monex"}
    assert database.read_bytes() == before
    with pytest.raises(ValueError, match="one exact source"):
        sources.inspect_ready(store, selected, confirmed_partial=True)
