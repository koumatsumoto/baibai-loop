from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.batch.external_assessment_publication_fixtures import pair_responses, upload_bundle
from tests.batch.test_external_assessment_daily import FakeObjectStore, completed_pair, plan
from tests.batch.test_external_assessment_publication import RECEIVED
from tests.batch.test_external_assessment_publication import publication as publication
from tests.engine.test_external_assessment_host_rate import Clock, Session
from tests.engine.test_external_assessment_sources import official_html, rakuten_html

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.jobs import external_assessment_publication as publication_module
from baibai_batch.jobs import external_assessment_sources as sources
from baibai_batch.jobs.external_assessment_cohorts import make_plan, new_result, record_outcome
from baibai_batch.observability.external_assessment_discord import render_sources
from baibai_batch.storage import external_assessment_staging as staging
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_engine.batch_api import (
    ExternalAssessmentCollectionOutcome,
    external_assessment_collector_binding,
    external_assessment_decode_attempt,
    external_assessment_history_digests,
)


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


@pytest.fixture
def independent_plans(publication):
    store, original, handle, database, mirror, directory = publication
    selected = []
    for source in ("monex", "rakuten"):
        p = make_plan(
            run_id=f"{source}-independent",
            sources=(source,),
            tickers=("6758", "7203"),
            universe_binding=original.universe_binding,
            collector_binding=external_assessment_collector_binding(source=source),
        )
        journal = new_result(p, 0)
        originals = []
        for _, ticker in p.cohorts[0].pairs:
            responses = pair_responses(
                source, ticker, received_at_utc=RECEIVED + timedelta(seconds=1)
            )
            originals.extend(responses)
            observation = external_assessment_decode_attempt(responses)
            journal = record_outcome(
                p,
                journal,
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", None, 200, responses[0].attempt_id
                ),
                observation,
            )
        key = upload_bundle(store, p, 0, tuple(originals))
        staging.save_new_plan(store, p)
        store.store_object(
            staging.stage_key(p, 0),
            staging.stage_bytes(p, staging.CohortStage(journal, raw_bundle_keys=(key,))),
        )
        selected.append({"source": source, "run_id": p.run_id})
    return store, selected, handle, database, mirror, directory


def publish(batch, **kwargs):
    store, selected, handle, database, mirror, directory = batch
    return sources.publish_ready(
        store,
        selected,
        handle=handle,
        sqlite_path=database,
        mirror=mirror,
        directory=directory / kwargs.pop("name", "combined-readback"),
        **kwargs,
    )


def test_two_independent_plans_share_one_release_and_keep_individual_terminals(
    independent_plans, monkeypatch
):
    store, selected, _, database, _, _ = independent_plans
    before = external_assessment_history_digests(database)
    calls = []
    original = publication_module.publish_market_lake

    def once(**kwargs):
        calls.append(True)
        return original(**kwargs)

    monkeypatch.setattr(publication_module, "publish_market_lake", once)
    report, ok = publish(independent_plans)
    assert ok
    assert calls == [True]
    assert [row["status"] for row in report["sources"]] == ["published", "published"]
    assert [row["rows_added"] for row in report["sources"]] == [2, 2]
    terminals = [
        staging.read_terminal(store, staging.read_plan(store, item["run_id"])) for item in selected
    ]
    assert terminals[0]["release_id"] == terminals[1]["release_id"]
    assert terminals[0]["plan_sha256"] != terminals[1]["plan_sha256"]
    assert all(item["expected_pairs"] == item["observations"] == 2 for item in terminals)
    assert set(before) <= set(external_assessment_history_digests(database))
    repeated, ok = publish(independent_plans, name="unused-sealed")
    assert ok
    assert calls == [True]
    assert all(row["readback"] == "previously_verified" for row in repeated["sources"])


def test_blocked_source_does_not_prevent_another_exact_plan_publication(independent_plans):
    store, selected, *_ = independent_plans
    p = make_plan(
        run_id="minkabu-blocked",
        sources=("minkabu",),
        tickers=("6758", "7203"),
        universe_binding="synthetic",
        collector_binding=external_assessment_collector_binding(source="minkabu"),
    )
    journal = record_outcome(
        p,
        new_result(p, 0),
        ExternalAssessmentCollectionOutcome(
            "minkabu", "6758", "access_denied", None, 403, str(uuid.uuid4())
        ),
    )
    staging.save_new_plan(store, p)
    store.store_object(
        staging.stage_key(p, 0), staging.stage_bytes(p, staging.CohortStage(journal))
    )
    selected.append({"source": "minkabu", "run_id": p.run_id})
    report, ok = publish(independent_plans)
    assert ok
    assert [row["status"] for row in report["sources"]] == ["published", "published", "blocked"]
    blocked = report["sources"][-1]
    assert blocked["state_counts"]["failure"] == 1
    assert blocked["state_counts"]["unattempted"] == 1
    assert staging.read_terminal(store, p) is None
    assert not any("minkabu-blocked/published.json" in key for key in store.put_keys)


def test_terminal_write_failure_keeps_sealed_source_and_retry_imports_remaining_idempotently(
    independent_plans, monkeypatch
):
    store, selected, _, database, _, _ = independent_plans
    write = publication_module.put_private

    def fail_second(store, key, *args, **kwargs):
        if key == staging.prefix(selected[1]["run_id"]) + "/published.json":
            raise LakePublishError("synthetic terminal response unavailable")
        return write(store, key, *args, **kwargs)

    monkeypatch.setattr(publication_module, "put_private", fail_second)
    first, ok = publish(independent_plans)
    assert not ok
    assert first["sources"][0]["status"] == "already_published"
    assert first["sources"][1]["status"] == "publication_unconfirmed"
    key = staging.prefix(selected[0]["run_id"]) + "/published.json"
    sealed = store.values[key].body
    history = external_assessment_history_digests(database)
    monkeypatch.setattr(publication_module, "put_private", write)
    second, ok = publish(independent_plans, name="retry-readback")
    assert ok
    assert second["sources"][0]["status"] == "already_published"
    assert second["sources"][1]["rows_added"] == 0
    assert store.values[key].body == sealed
    assert external_assessment_history_digests(database) == history


def test_corrupt_selected_raw_does_not_publish_any_terminal(independent_plans):
    store, selected, _, database, _, _ = independent_plans
    p = staging.read_plan(store, selected[-1]["run_id"])
    stage, _ = staging.read_stage(store, p, 0)
    store.store_object(stage.raw_bundle_keys[0], b"invalid archive")
    history = external_assessment_history_digests(database)
    pointer_before = store.values["lake/pointers/market/current.json"].body
    report, ok = publish(independent_plans)
    assert not ok
    assert all(row["status"] == "publication_unconfirmed" for row in report["sources"])
    assert store.values["lake/pointers/market/current.json"].body == pointer_before
    assert external_assessment_history_digests(database) == history
    assert all(
        staging.read_terminal(store, staging.read_plan(store, item["run_id"])) is None
        for item in selected
    )


def test_manual_scope_does_not_publish_other_ready_source_and_partial_requires_one_plan(
    independent_plans,
):
    store, selected, handle, database, mirror, directory = independent_plans
    with pytest.raises(ValueError, match="one exact source"):
        sources.publish_ready(
            store,
            selected,
            handle=handle,
            sqlite_path=database,
            mirror=mirror,
            directory=directory / "refused",
            confirmed_partial=True,
        )
    one = (store, selected[:1], handle, database, mirror, directory)
    report, ok = publish(one)
    assert ok
    assert len(report["sources"]) == 1
    assert staging.read_terminal(store, staging.read_plan(store, selected[1]["run_id"])) is None


def test_explicit_partial_closes_only_one_source_and_preserves_unattempted_pairs(
    independent_plans,
):
    store, selected, handle, database, mirror, directory = independent_plans
    p = staging.read_plan(store, selected[0]["run_id"])
    existing, _ = staging.read_stage(store, p, 0)
    # Keep one original observation and leave the second fixed pair unattempted.
    journal = new_result(p, 0)
    journal = replace(journal, entries=(existing.result.entries[0], journal.entries[1]))
    store.store_object(
        staging.stage_key(p, 0),
        staging.stage_bytes(p, replace(existing, result=journal)),
    )
    one = (store, selected[:1], handle, database, mirror, directory)
    normal, ok = publish(one, name="normal-refused")
    assert not ok
    assert normal["sources"][0]["status"] == "incomplete"
    partial, ok = publish(one, name="explicit-partial", confirmed_partial=True)
    assert ok
    assert partial["sources"][0]["status"] == "partially_published"
    terminal = staging.read_terminal(store, p)
    assert terminal["state_counts"]["unattempted"] == 1
    assert terminal["observations"] == 1
    assert staging.read_terminal(store, staging.read_plan(store, selected[1]["run_id"])) is None
    repeated, ok = publish(one, name="closed-source")
    assert ok
    assert repeated["sources"][0]["status"] == "already_partially_published"


def test_sealed_source_inspection_reads_no_stage_or_raw(independent_plans, monkeypatch, tmp_path):
    store, selected, *_ = independent_plans
    _, ok = publish(independent_plans)
    assert ok

    def forbid(*args, **kwargs):
        pytest.fail("sealed source must not read stages or require a fresh market")

    monkeypatch.setattr(sources, "read_stages", forbid)
    monkeypatch.setattr(sources, "prepare_publication", forbid)
    monkeypatch.setattr(sources, "publish_plans_and_verify", forbid)
    ready, reports = sources.inspect_ready(store, selected)
    assert ready == []
    assert all(row["status"] == "already_published" for row in reports.values())
    report, ok = publish(independent_plans, name="unused-no-hydrate")
    assert ok
    assert all(row["readback"] == "previously_verified" for row in report["sources"])

    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: store)
    monkeypatch.setattr(sources, "_load_handle", forbid)
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
    assert all(
        row["readback"] == "previously_verified"
        for row in json.loads(result_path.read_text())["sources"]
    )


def test_source_notice_never_reports_old_values_as_new_success(tmp_path):
    path = tmp_path / "source-results.json"
    path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source": "monex",
                        "status": "already_published",
                        "expected_pairs": 2,
                        "observations": 2,
                        "rows_added": 0,
                    },
                    {
                        "source": "minkabu",
                        "status": "blocked",
                        "expected_pairs": 2,
                        "state_counts": {
                            "success": 0,
                            "normal_null": 0,
                            "failure": 1,
                            "unattempted": 1,
                        },
                    },
                ]
            }
        )
    )
    text = render_sources(path, lease_release="success", cancelled="false")
    assert text.startswith("[DEGRADED]")
    assert "以前の公開確認済み・今回再公開なし" in text
    assert "失敗=1" in text
    assert "未試行=1" in text
    assert "monex: " in text
    assert "minkabu: " in text
    assert "正常null=不明" in text


def test_workflow_has_four_source_jobs_and_one_serial_publisher():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"
    data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    group = data["concurrency"]
    assert group["queue"] == "max"
    assert group["cancel-in-progress"] == "false"
    assert "inputs.operation == 'publish_confirmed_partial'" in group["group"]
    assert "format('external-assessment-acquire-{0}', inputs.source)" in group["group"]
    assert "format('external-assessment-run-{0}', github.run_id)" in group["group"]
    acquire = data["jobs"]["acquire"]
    assert acquire["strategy"]["fail-fast"] == "false"
    assert set(acquire["strategy"]["matrix"]) == {"include"}
    assert acquire["concurrency"]["group"] == "external-assessment-acquire-${{ matrix.source }}"
    publisher = data["jobs"]["publish"]
    assert publisher["concurrency"]["group"] == "cloud-publish"
    assert "always()" in publisher["if"]
    assert "needs.acquire.result" not in publisher["if"]
    assert "cohort" not in str(acquire["strategy"]["matrix"])
    steps = publisher["steps"]
    ready = next(step for step in steps if step.get("id") == "ready")
    assert "external_assessment_sources inspect" in ready["run"]
    for step in steps:
        if any(
            token in step.get("run", "")
            for token in (
                "publication_lease acquire",
                "r2_transfer.sh pull-market",
                "external_assessment_sources publish",
            )
        ):
            assert step["if"] == "${{ steps.ready.outputs.ready == 'true' }}"
