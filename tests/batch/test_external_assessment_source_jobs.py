from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
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
from baibai_batch.jobs.external_assessment_cohorts import (
    make_plan,
    new_result,
    plan_bytes,
    record_outcome,
)
from baibai_batch.observability import external_assessment_discord as notify
from baibai_batch.observability.external_assessment_discord import render_sources
from baibai_batch.storage import external_assessment_staging as staging
from baibai_batch.storage import publish_market_lake as lake_publisher
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.publication_lease import acquire, release
from baibai_engine.batch_api import (
    ExternalAssessmentCollectionOutcome,
    LakeObjectCache,
    LocalMirrorSource,
    create_market_snapshot,
    equity_web_history_digests,
    external_assessment_collector_binding,
    external_assessment_decode_attempt,
    external_assessment_history_digests,
    lake_session,
    read_lake_store_origin,
)
from baibai_engine.storage.lake.hydrate import dehydrate_market_store, hydrate_market_store


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
    store, _original, handle, database, mirror, directory = publication
    selected = []
    for source in ("monex", "rakuten"):
        p = make_plan(
            run_id=f"{source}-independent",
            sources=(source,),
            tickers=("6758", "7203"),
            universe_binding=json.dumps({"master_asof": "2026-10-09"}),
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


def source_notice(report, tmp_path, monkeypatch, *, publish_result):
    path = tmp_path / "notice-source-report.json"
    path.write_text(json.dumps(report))
    output = tmp_path / "notice-message-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def forbid(*args):
        pytest.fail("publisher renders without sending")

    assert (
        notify.main(
            [
                "--render-only",
                "--sources-report",
                str(path),
                "--publish-result",
                publish_result,
                "--lease-release-outcome",
                "success",
            ],
            transport=forbid,
        )
        == 0
    )
    prepared = output.read_text().rstrip("\n").split("=", 1)[1]
    monkeypatch.delenv("GITHUB_OUTPUT")
    monkeypatch.setenv(
        "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/synthetic-secret"
    )
    messages = []

    def transport(url, body, timeout):
        payload = json.loads(body)
        assert payload["allowed_mentions"] == {"parse": []}
        messages.append(payload["content"])
        return 200

    assert notify.main(["--message-json", prepared], transport=transport) == 0
    assert len(messages) == 1
    assert "synthetic-secret" not in messages[0]
    return messages[0]


def test_two_independent_plans_share_one_release_and_keep_individual_terminals(
    independent_plans, monkeypatch, tmp_path
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
    message = source_notice(report, tmp_path, monkeypatch, publish_result="success")
    assert message.startswith("[OK]")
    assert "以前の" not in message
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
    repeated_notice = source_notice(repeated, tmp_path, monkeypatch, publish_result="skipped")
    assert repeated_notice.startswith("[SKIPPED]")
    assert "追加row=0" in repeated_notice


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


def test_terminal_write_failure_resumes_from_fresh_hydrate_without_duplicate_history(
    independent_plans, monkeypatch, tmp_path
):
    store, selected, handle, database, mirror, directory = independent_plans
    before = lake_publisher._serving_pointer(store).pointer
    assert before is not None
    # pull-market restores store-local acquisition coverage; the Lake owns the
    # published rows. Keep the remote machine snapshot from before this attempt.
    metadata_snapshot = directory / "market-metadata.sqlite"
    create_market_snapshot(database, metadata_snapshot)
    base = lake_publisher._resolve_serving_release(store, mirror, before)
    dehydrate_market_store(metadata_snapshot, release=base)
    with closing(sqlite3.connect(metadata_snapshot)) as connection:
        coverage = connection.execute(
            "SELECT * FROM source_coverage ORDER BY source, coverage_key"
        ).fetchall()
    assert coverage
    store.store_object(
        "market.sqlite", metadata_snapshot.read_bytes(), content_type="application/octet-stream"
    )
    write = publication_module.put_private

    def fail_second(store, key, *args, **kwargs):
        if key == staging.prefix(selected[1]["run_id"]) + "/published.json":
            raise LakePublishError("synthetic terminal response unavailable")
        return write(store, key, *args, **kwargs)

    monkeypatch.setattr(publication_module, "put_private", fail_second)
    first, ok = publish(independent_plans)
    assert not ok
    assert first["sources"][0]["status"] == "published"
    assert first["sources"][0]["readback"] == "verified"
    assert first["sources"][0]["rows_added"] is None
    assert first["sources"][0]["source_responses_added"] is None
    assert first["sources"][1]["status"] == "publication_unconfirmed"
    first_notice = source_notice(first, tmp_path, monkeypatch, publish_result="failure")
    assert first_notice.startswith("[DEGRADED]")
    assert "step失敗" in first_notice
    assert "追加row=不明" in first_notice
    assert "monex: 公開確認済み" in first_notice
    assert "以前の" not in first_notice
    published = lake_publisher._serving_pointer(store).pointer
    assert published is not None
    assert published != before
    key = staging.prefix(selected[0]["run_id"]) + "/published.json"
    sealed = store.values[key].body
    history = external_assessment_history_digests(database)
    source_history = equity_web_history_digests(database, months={(2026, 9)})

    # A new runner pulls the unchanged machine snapshot, then fills its empty
    # Lake tables from the exact graph already published to MemoryR2.
    fresh_database = directory / "fresh-market.sqlite"
    store.download_file("market.sqlite", fresh_database)
    with closing(sqlite3.connect(fresh_database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone() == (0,)
    fresh_mirror = directory / "fresh-mirror"
    fixed = lake_publisher._resolve_serving_release(store, fresh_mirror, published)
    for manifest in fixed.dataset_manifests.values():
        for partition in manifest.partitions:
            for obj in partition.objects:
                lake_publisher._fetch(store, fresh_mirror, obj.key, expected_sha256=obj.sha256)
    with lake_session() as session:
        hydrated = hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=fresh_mirror, source=LocalMirrorSource(fresh_mirror)),
            store=fresh_database,
            dataset_names=fixed.dataset_names(),
        )
    assert (hydrated.release_id, hydrated.release_manifest_sha256) == (
        published.release_id,
        published.manifest_sha256,
    )
    origin = read_lake_store_origin(fresh_database)
    assert origin is not None
    assert (origin.release_id, origin.release_manifest_sha256) == (
        published.release_id,
        published.manifest_sha256,
    )
    assert external_assessment_history_digests(fresh_database) == history
    assert equity_web_history_digests(fresh_database, months={(2026, 9)}) == source_history
    with closing(sqlite3.connect(fresh_database)) as connection:
        assert (
            connection.execute(
                "SELECT * FROM source_coverage ORDER BY source, coverage_key"
            ).fetchall()
            == coverage
        )
    release(store, handle)
    new_handle, _ = acquire(store, "operator")

    monkeypatch.setattr(publication_module, "put_private", write)
    resume_errors = []
    publish_plans = sources.publish_plans_and_verify

    def diagnose_resume(*args, **kwargs):
        try:
            return publish_plans(*args, **kwargs)
        except Exception as error:
            resume_errors.append(f"{type(error).__name__}: {error}")
            raise

    monkeypatch.setattr(sources, "publish_plans_and_verify", diagnose_resume)
    restarted = (store, selected, new_handle, fresh_database, fresh_mirror, directory)
    second, ok = publish(restarted, name="retry-readback")
    assert ok, (second, resume_errors)
    assert second["sources"][0]["status"] == "already_published"
    assert second["sources"][1]["status"] == "published"
    assert second["sources"][1]["readback"] == "verified"
    assert second["sources"][1]["rows_added"] == 0
    assert second["sources"][1]["source_responses_added"] == 0
    second_notice = source_notice(second, tmp_path, monkeypatch, publish_result="success")
    assert "monex: 以前の公開確認済み" in second_notice
    assert "rakuten: 公開確認済み" in second_notice
    assert store.values[key].body == sealed
    assert external_assessment_history_digests(fresh_database) == history
    assert equity_web_history_digests(fresh_database, months={(2026, 9)}) == source_history


@pytest.mark.parametrize("partial", [False, True])
def test_terminal_written_before_exception_is_current_publication_with_unknown_additions(
    independent_plans, monkeypatch, tmp_path, partial
):
    store, selected, handle, database, mirror, directory = independent_plans
    one = (store, selected[:1], handle, database, mirror, directory)
    original = publication_module.put_private

    def saved_then_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise LakePublishError("synthetic terminal acknowledgement lost")

    monkeypatch.setattr(publication_module, "put_private", saved_then_lost)
    report, ok = publish(one, confirmed_partial=partial)
    assert not ok
    row = report["sources"][0]
    assert row["status"] == ("partially_published" if partial else "published")
    assert row["readback"] == "verified"
    assert row["rows_added"] is None
    assert row["source_responses_added"] is None
    message = source_notice(report, tmp_path, monkeypatch, publish_result="failure")
    assert message.startswith("[DEGRADED]")
    assert "step失敗" in message
    assert "追加row=不明" in message
    assert "以前の" not in message
    assert "未公開" not in message
    monkeypatch.setattr(publication_module, "put_private", original)
    repeated, ok = publish(one, name="terminal-read-only-retry")
    assert ok
    assert repeated["sources"][0]["status"] == (
        "already_partially_published" if partial else "already_published"
    )
    assert repeated["sources"][0]["readback"] == "previously_verified"
    assert repeated["sources"][0]["rows_added"] == 0
    repeated_notice = source_notice(repeated, tmp_path, monkeypatch, publish_result="skipped")
    assert repeated_notice.startswith("[SKIPPED]")
    assert "追加row=0" in repeated_notice


@pytest.mark.parametrize("success", [False, True])
def test_source_publish_cli_writes_current_result_only_after_publisher_returns(
    monkeypatch, tmp_path, success
):
    path = tmp_path / "source-report.json"
    expected = {"sources": [{"source": "monex", "status": "published", "rows_added": None}]}
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: object())
    monkeypatch.setattr(sources, "_load_handle", lambda *args: object())

    def publish(*args, **kwargs):
        assert not path.exists()
        return expected, success

    monkeypatch.setattr(sources, "publish_ready", publish)
    assert sources.main(
        [
            "publish",
            "--selection-json",
            '[{"source":"monex","run_id":"monex-current"}]',
            "--directory",
            str(tmp_path / "readback"),
            "--lease-handle",
            str(tmp_path / "lease"),
            "--report-output",
            str(path),
        ]
    ) == (0 if success else 1)
    assert json.loads(path.read_text()) == expected


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
    _, ok = publish(one, confirmed_partial=partial)
    assert ok
    terminal_key = staging.prefix(p.run_id) + "/published.json"
    sealed_bytes = store.values[terminal_key].body
    store.put_keys.clear()
    store.get_keys.clear()
    store.head_keys.clear()

    def forbid(*args, **kwargs):
        pytest.fail("sealed source cannot touch stage/HTTP/lease/DB/current release")

    for name in ("read_stages", "prepare_publication", "publish_plans_and_verify", "_load_handle"):
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
    report, ok = publish(one, name="unused-no-hydrate")
    assert ok
    assert report["sources"][0]["readback"] == "previously_verified"
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


def test_workflow_publishes_each_source_without_waiting_for_its_siblings():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-external-assessment.yml"
    parent = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
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
    assert "inputs.operation == 'collect_and_publish'" in acquire["if"]
    assert "cloud-publish" not in str(acquire)
    assert "publish" not in child["jobs"]
    handoff = child["jobs"]["handoff"]
    assert handoff["needs"] == "acquire"
    assert "always()" in handoff["if"]
    assert "needs.acquire.result" not in handoff["if"]
    ready = next(step for step in handoff["steps"] if step.get("id") == "ready")
    assert "external_assessment_sources ready" in ready["run"]
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
