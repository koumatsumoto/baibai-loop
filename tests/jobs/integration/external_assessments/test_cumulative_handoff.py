"""累積原本の反映は、取得planの終了ではない。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.external_assessment_publication import (
    RECEIVED,
    _plan,
    _stage,
    pair_responses,
)
from tests.helpers.external_assessment_publication import publication as publication

import baibai_jobs.acquisition.external_assessments.staging as staging
import baibai_jobs.publication.snapshot_publication as publisher
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import (
    decode_attempt,
)
from baibai_jobs.integration.external_assessment_ready import (
    available_ready,
    load_ready,
    prepare_plan,
    read_acknowledgement,
    save_ready,
)
from baibai_jobs.integration.external_assessments import merge_ready, verify_and_seal


def make_partial(publication):
    store, previous, *_ = publication
    plan = _plan(
        previous,
        run_id="cumulative-monex",
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


def test_partial_ack_keeps_plan_open_and_later_superset_integrates(publication):
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
    assert available_ready(store) == ([], [])
    assert merge_ready(store, [first], database=database)["plans"] == []

    # 短期handoffが失効しても、先に公開済みの原受領はcanonical hydrateから再読する。
    for key in load_ready(store, first).stages[0][0].raw_bundle_keys:
        del store.values[key]
    add_cohort(store, plan, 1)
    second = save_ready(store, prepare_plan(store, plan, cumulative=True))
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


def test_cumulative_inspect_does_not_require_all_cohorts(publication):
    from baibai_jobs.acquisition.external_assessments.sources import inspect_ready

    store, plan = make_partial(publication)
    selected = [{"source": "monex", "run_id": plan.run_id}]
    assert inspect_ready(store, selected)[0] == []
    prepared, reports = inspect_ready(store, selected, cumulative=True)
    assert len(prepared) == 1
    assert prepared[0].cumulative
    assert reports["monex"]["state_counts"]["unattempted"] == 1


def test_invalid_ack_cannot_silently_remove_ready(publication):
    from baibai_jobs.integration.external_assessment_ready import acknowledgement_key

    store, plan = make_partial(publication)
    reference = save_ready(store, prepare_plan(store, plan, cumulative=True))
    store.store_object(acknowledgement_key(plan, reference), b"[]")
    assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])
