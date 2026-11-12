"""Freezeの一走査だけで、検証済みの不変入力を共有する。"""

import json
from collections import Counter

import pytest
from tests.helpers.external_assessment_publication import publication as publication
from tests.jobs.integration.external_assessments.test_cumulative_handoff import (
    add_cohort,
    make_partial,
)

from baibai_jobs.acquisition.external_assessments.staging import prefix
from baibai_jobs.integration.external_assessment_ready import (
    acknowledgement_key,
    available_ready,
    load_ready,
    prepare_plan,
    save_ready,
)


def cumulative_pair(publication):
    store, plan = make_partial(publication)
    first = save_ready(store, prepare_plan(store, plan, cumulative=True))
    add_cohort(store, plan, 1)
    second = save_ready(store, prepare_plan(store, plan, cumulative=True))
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    return store, plan, first, second


def test_scan_reads_each_immutable_once_and_keeps_mutable_marker_checks(publication):
    store, plan, first, second = cumulative_pair(publication)
    assert available_ready(store) == ([second], [])
    reads = Counter(store.get_keys)
    assert len(reads) == 5  # plan、ready二件、共有stageを含むstage二件
    assert set(reads.values()) == {1}
    assert reads[first["key"]] == reads[second["key"]] == 1
    assert reads[prefix(plan.run_id) + "/plan.json"] == 1
    heads = Counter(store.head_keys)
    assert all(heads[key] == 2 for key in reads)
    assert heads[prefix(plan.run_id) + "/published.json"] == 2
    assert heads[acknowledgement_key(plan, second)] == 1
    assert len(store.head_keys) == 13
    assert store.put_keys == []

    # 別のFreezeでは同じ固定入力もremoteから検証し直す。
    store.get_keys.clear()
    store.head_keys.clear()
    assert available_ready(store) == ([second], [])
    assert Counter(store.get_keys) == reads
    assert Counter(store.head_keys) == heads


@pytest.mark.parametrize("target", ["plan", "ready", "stage"])
def test_next_scan_and_independent_load_reject_changed_immutable(publication, target):
    store, plan, _, reference = cumulative_pair(publication)
    assert available_ready(store) == ([reference], [])
    key = {
        "plan": prefix(plan.run_id) + "/plan.json",
        "ready": reference["key"],
        "stage": json.loads(store.values[reference["key"]].body)["stages"][0]["key"],
    }[target]
    store.store_object(key, b"{}")
    pending, failed = available_ready(store)
    assert pending == []
    assert failed
    assert all(entry["run_id"] == plan.run_id for entry in failed)
    with pytest.raises((ValueError, KeyError)):
        load_ready(store, reference)
    assert store.put_keys == []


@pytest.mark.parametrize("changed", ["run_id", "source", "stage_hash", "stage_index"])
def test_shared_stage_does_not_skip_each_ready_binding(publication, monkeypatch, changed):
    store, plan, first, second = cumulative_pair(publication)
    value = json.loads(store.values[second["key"]].body)
    if changed in {"run_id", "source"}:
        value[changed] = "other"
    elif changed == "stage_hash":
        value["stages"][0]["sha256"] = "0" * 64
    else:
        value["stages"][0]["index"] = 1
    store.store_object(second["key"], json.dumps(value).encode())
    # 必ず正常なfirstからstageを共有した後に、別の参照を検証する。
    monkeypatch.setattr(store, "iter_objects", lambda _: [(first["key"], 0), (second["key"], 0)])
    assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])
    shared_key = json.loads(store.values[first["key"]].body)["stages"][0]["key"]
    assert store.get_keys.count(shared_key) == 1


def test_new_terminal_during_scan_is_not_hidden_by_shared_plan(publication, monkeypatch):
    store, plan, first, second = cumulative_pair(publication)

    def listed(_):
        yield first["key"], 0
        store.store_object(prefix(plan.run_id) + "/published.json", b"{}")
        yield second["key"], 0

    monkeypatch.setattr(store, "iter_objects", listed)
    assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])
    assert store.get_keys.count(prefix(plan.run_id) + "/plan.json") == 1
    assert prefix(plan.run_id) + "/published.json" in store.get_keys


def test_new_ack_during_scan_is_read_after_immutable_selection(publication, monkeypatch):
    store, plan, first, second = cumulative_pair(publication)

    def listed(_):
        yield first["key"], 0
        yield second["key"], 0
        store.store_object(acknowledgement_key(plan, second), b"{}")

    monkeypatch.setattr(store, "iter_objects", listed)
    assert available_ready(store) == ([], [{"run_id": plan.run_id, "reason": "ValueError"}])
    assert acknowledgement_key(plan, second) in store.get_keys
