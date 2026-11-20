"""累積原本の反映は、取得planの終了ではない。"""

import hashlib
import json
import multiprocessing
import os
import shelve
import signal
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.external_assessment_publication import (
    RECEIVED,
    _plan,
    _stage,
    pair_responses,
    prepare_plan,
)
from tests.helpers.external_assessment_publication import publication as publication
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.acquisition.external_assessments.staging as staging
import baibai_jobs.publication.snapshot_publication as publisher
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_jobs.integration.external_assessment_ready import (
    acknowledgement_key,
    available_ready,
    load_ready,
    read_acknowledgement,
    save_ready,
)
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal


def make_partial(publication, *, run_id="cumulative-monex"):
    store, previous, *_ = publication
    plan = _plan(
        previous,
        run_id=run_id,
        sources=("monex",),
        tickers=("6758", "7203"),
        cohort_size=1,
    )
    staging.save_new_plan(store, plan)
    add_cohort(store, plan, 0)
    return store, plan


def add_cohort(store, plan, index, *, seconds=0):
    source, ticker = plan.cohorts[index].pairs[0]
    responses = pair_responses(
        source, ticker, received_at_utc=RECEIVED + timedelta(seconds=seconds)
    )
    _stage(
        store,
        plan,
        index,
        [(source, ticker, "observed", responses[0].attempt_id, decode_attempt(responses), 200)],
        responses,
    )


def test_partial_ack_keeps_plan_open_and_later_superset_integrates(publication, monkeypatch):
    store, plan = make_partial(publication)
    _, _, handle, database, mirror, directory = publication
    first = save_ready(store, prepare_plan(store, plan, cumulative=True))
    state = merge_ready(store, [first], database=database)
    assert state["failed"] == []
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    verify_and_seal(store, state, handle=handle, directory=directory / "first")
    assert staging.read_terminal(store, plan) is None
    ack = read_acknowledgement(store, load_ready(store, first), first)
    assert ack["state_counts"]["unattempted"] == 1
    store.get_keys.clear()
    assert available_ready(store) == ([], [])
    assert acknowledgement_key(plan, first) in store.get_keys
    assert not any("/confirmed-stages/" in key for key in store.get_keys)

    # Same key with different ready bytes has no receipt. Explicit replay always needs the stage.
    marker = json.loads(store.values[first["key"]].body)
    with monkeypatch.context() as patch:
        stage = marker["stages"][0]["key"]
        patch.delitem(store.values, stage)
        assert available_ready(store) == ([], [])
        with pytest.raises(ValueError, match="confirmed external cohort snapshot changed"):
            load_ready(store, first)
        patch.delitem(store.values, acknowledgement_key(plan, first))
        assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])
    with monkeypatch.context() as patch:
        patch.delitem(store.values, first["key"])
        marker["created_at"] = "2026-01-01T00:00:00Z"
        changed = json.dumps(marker, sort_keys=True).encode()
        store.store_object(first["key"], changed)
        assert available_ready(store) == (
            [{"key": first["key"], "sha256": hashlib.sha256(changed).hexdigest()}],
            [],
        )
    assert merge_ready(store, [first], database=database)["plans"] == []

    # 短期handoffが失効しても、先に公開済みの原受領はcanonical hydrateから再読する。
    for key in load_ready(store, first).stages[0][0].raw_bundle_keys:
        del store.values[key]
    add_cohort(store, plan, 1)
    store.get_keys.clear()
    second = save_ready(store, prepare_plan(store, plan, cumulative=True))
    assert store.get_keys.count(acknowledgement_key(plan, first)) == 1
    assert available_ready(store) == ([second], [])
    state = merge_ready(store, [second], database=database)
    assert state["failed"] == []
    assert state["plans"][0]["source_responses_added"] == 1
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    verify_and_seal(store, state, handle=handle, directory=directory / "second")
    assert staging.read_terminal(store, plan) is None
    assert read_acknowledgement(store, load_ready(store, first), first) == ack
    assert available_ready(store) == ([], [])


def test_late_subset_never_replaces_superset(publication):
    store, plan = make_partial(publication)
    small = prepare_plan(store, plan, cumulative=True)
    add_cohort(store, plan, 1)
    large = save_ready(store, prepare_plan(store, plan, cumulative=True))
    late = save_ready(store, small)
    assert late != large
    assert available_ready(store) == ([large], [])
    assert save_ready(store, small) == late


def test_incomparable_ready_is_reported_without_arbitrary_clock_selection(publication):
    store, plan = make_partial(publication)
    first = prepare_plan(store, plan, cumulative=True)
    save_ready(store, first)
    add_cohort(store, plan, 0, seconds=1)
    save_ready(store, prepare_plan(store, plan, cumulative=True))
    pending, failures = available_ready(store)
    assert pending == []
    assert failures == [{"run_id": plan.run_id, "reason": "ValueError"}]


def test_raw_only_parse_failure_can_be_handed_off_and_not_marked_success(publication):
    store, plan = make_partial(publication)
    from baibai_jobs.acquisition.external_assessments.cohorts import PairEntry

    previous, _ = staging.read_stage(store, plan, 0)
    entry = previous.result.entries[0]
    failed = PairEntry(
        entry.source,
        entry.ticker,
        "failure",
        "missing_theory_table",
        200,
        attempt_id=entry.attempt_id,
    )
    changed = replace(previous, result=replace(previous.result, entries=(failed,)))
    store.store_object(staging.stage_key(plan, 0), staging.stage_bytes(plan, changed))
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    _, _, handle, database, mirror, directory = publication
    state = merge_ready(store, [reference], database=database)
    assert state["failed"] == []
    assert state["plans"][0]["rows_added"] == 0
    assert state["plans"][0]["source_responses_added"] == 1
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    verify_and_seal(store, state, handle=handle, directory=directory / "raw-only")
    ack = read_acknowledgement(store, load_ready(store, reference), reference)
    assert ack["observations"] == 0
    assert available_ready(store) == ([], [])
    assert ack["state_counts"] == {"failure": 1, "unattempted": 1, "success": 0, "normal_null": 0}
    assert staging.read_terminal(store, plan) is None


def test_empty_or_explicit_close_is_not_cumulative(publication):
    store, old, *_ = publication
    plan = _plan(old, run_id="empty-cumulative", sources=("monex",))
    staging.save_new_plan(store, plan)
    with pytest.raises(ValueError, match="at least one attempted"):
        prepare_plan(store, plan, cumulative=True)
    with pytest.raises(ValueError, match="cannot close"):
        prepare_plan(store, plan, cumulative=True, confirmed_partial=True)


def test_ack_write_response_loss_is_idempotent_without_closing_plan(publication, monkeypatch):
    import baibai_jobs.integration.external_assessments as integration
    from baibai_engine.storage.objects.r2 import LakePublishError

    store, plan = make_partial(publication)
    _, _, handle, database, mirror, directory = publication
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    state = merge_ready(store, [reference], database=database)
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)
    put = integration.put_private

    def lost_response(*args, **kwargs):
        put(*args, **kwargs)
        raise LakePublishError("ack response lost")

    monkeypatch.setattr(integration, "put_private", lost_response)
    with pytest.raises(LakePublishError, match="ack response lost"):
        verify_and_seal(store, state, handle=handle, directory=directory / "lost")
    assert available_ready(store) == ([], [])
    assert merge_ready(store, [reference], database=database)["plans"] == []
    assert staging.read_terminal(store, plan) is None


def test_failed_readback_does_not_acknowledge_partial_originals(publication, monkeypatch):
    import baibai_jobs.integration.external_assessments as integration

    store, plan = make_partial(publication)
    _, _, handle, database, mirror, directory = publication
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    state = merge_ready(store, [reference], database=database)
    publisher.publish_market_lake(sqlite_path=database, mirror_root=mirror, store=store)

    def reject(*args, **kwargs):
        raise ValueError("independent native verification failed")

    monkeypatch.setattr(integration, "equity_web_verify_release_history", reject)
    with pytest.raises(ValueError, match="native verification"):
        verify_and_seal(store, state, handle=handle, directory=directory / "rejected")
    assert read_acknowledgement(store, load_ready(store, reference), reference) is None
    assert available_ready(store) == ([reference], [])


def test_cumulative_inspect_freezes_reported_stages_and_later_reads_progress(
    publication, monkeypatch
):
    import baibai_jobs.acquisition.external_assessments.sources as sources

    store, plan = make_partial(publication)
    selected = [{"source": "monex", "run_id": plan.run_id}]
    assert sources.inspect_ready(store, selected)[0] == []
    read_stages = sources.read_stages

    def advance_after_read(store, plan):
        saved = read_stages(store, plan)
        add_cohort(store, plan, 1, seconds=1)
        return saved

    with monkeypatch.context() as patch:
        patch.setattr(sources, "read_stages", advance_after_read)
        prepared, reports = sources.inspect_ready(store, selected, cumulative=True)
    assert len(prepared) == 1
    assert prepared[0].cumulative
    assert prepared[0].accounting["state_counts"] == reports["monex"]["state_counts"]
    assert reports["monex"]["state_counts"]["unattempted"] == 1
    assert set(prepared[0].stages) == {0}
    first = save_ready(store, prepared[0])
    assert set(load_ready(store, first).stages) == {0}

    progressed, reports = sources.inspect_ready(store, selected, cumulative=True)
    assert reports["monex"]["state_counts"]["unattempted"] == 0
    assert set(progressed[0].stages) == {0, 1}
    second = save_ready(store, progressed[0])
    assert available_ready(store) == ([second], [])


def _kill_handoff_at_write(remote, args, phase):
    import baibai_jobs.acquisition.external_assessments.sources as sources

    # Only service bytes survive this process. Do not simulate kill by throwing
    # inside main: that would still allow error handling and reporting to finish.
    with shelve.open(str(remote), flag="w") as objects:
        store = MemoryR2Store()
        store.values = objects
        put = store.put_file

        def stop():
            objects.sync()
            os.kill(os.getpid(), signal.SIGKILL)

        def interrupt(key, *args, **kwargs):
            if phase == "before_freeze" and "/confirmed-stages/" in key:
                stop()
            result = put(key, *args, **kwargs)
            if (phase == "stage_only" and "/confirmed-stages/" in key) or (
                phase == "lost_ready_response" and "/ready-" in key
            ):
                stop()
            return result

        store.put_file = interrupt
        sources.Boto3R2Store = lambda **_: store
        raise SystemExit(sources.main(args))


@pytest.mark.parametrize("phase", ["before_freeze", "stage_only", "lost_ready_response"])
def test_killed_ready_worker_cannot_substitute_later_mutable_stages(
    publication, tmp_path, phase, monkeypatch
):
    import baibai_jobs.acquisition.external_assessments.sources as sources

    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    store, plan = make_partial(publication, run_id="monex-kill")
    remote, report = tmp_path / "synthetic-store", tmp_path / "worker-report.json"
    with shelve.open(str(remote)) as objects:
        objects.update(store.values)
    exact = json.dumps([{"source": "monex", "run_id": plan.run_id}])
    args = ["ready", "--selection-json", exact, "--cumulative", "--report-output"]
    ctx = multiprocessing.get_context("spawn")
    worker = ctx.Process(target=_kill_handoff_at_write, args=(remote, [*args, str(report)], phase))
    worker.start()
    try:
        worker.join(30)
        assert worker.exitcode == -signal.SIGKILL
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(5)
    assert not report.exists()

    # dbm.sqlite3's read-only mode opens immutable=1, omitting a killed
    # writer's committed WAL; use the normal existing-store view.
    with shelve.open(str(remote), flag="w") as objects:
        store.values = dict(objects)
    confirmed = [key for key in store.values if "/confirmed-stages/" in key]
    assert len(confirmed) == int(phase != "before_freeze")
    add_cohort(store, plan, 1, seconds=1)
    store.put_keys.clear()
    refs, bad = available_ready(store)
    assert not bad
    if phase == "lost_ready_response":
        (fixed,) = refs
        frozen = load_ready(store, fixed)
        assert frozen.plan == plan
        assert set(frozen.stages) == {0}
        assert save_ready(store, frozen) == fixed  # same exact body, no extra PUT
    else:
        assert not refs  # even a confirmed stage is not a ready selection
    assert store.put_keys == []
    # The old separate runner could announce this larger set. It is not the
    # killed worker's result, even though source and resolved run ID both match.
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    legacy_report = tmp_path / "separate-runner.json"
    assert sources.main([*args, str(legacy_report)]) == 0
    (later,) = json.loads(legacy_report.read_bytes())["ready_handoffs"]
    assert later not in refs
    assert set(load_ready(store, later).stages) == {0, 1}


def test_invalid_ack_cannot_silently_remove_ready(publication):
    store, plan = make_partial(publication)
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    store.store_object(acknowledgement_key(plan, reference), b"[]")
    assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])


def test_missing_unacknowledged_original_never_seals_or_invents_zero(publication):
    store, plan = make_partial(publication)
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    for key in load_ready(store, reference).stages[0][0].raw_bundle_keys:
        del store.values[key]
    database = publication[3]
    state = merge_ready(store, [reference], database=database)
    assert state["plans"] == []
    assert state["failed"] == [{"reference": reference, "reason": "ValueError"}]
    assert read_acknowledgement(store, load_ready(store, reference), reference) is None
