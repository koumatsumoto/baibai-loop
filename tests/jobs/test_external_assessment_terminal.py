from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.acquisition.external_assessments.daily as daily
from baibai_jobs.acquisition.external_assessments.cohorts import make_plan
import baibai_jobs.acquisition.external_assessments.staging as staging


def plan():
    return make_plan(
        run_id="synthetic-terminal",
        sources=("monex", "rakuten"),
        tickers=("7203", "9432"),
        universe_binding=json.dumps({"master_asof": "2026-10-09"}),
        collector_binding=daily.external_assessment_collector_binding(),
    )


def terminal(p, *, partial=False):
    result = {
        "format": "external-assessment-published-batch-v1",
        "plan_sha256": p.plan_sha256,
        "release_id": "synthetic-retail-release",
        "release_manifest_sha256": "a" * 64,
        "expected_pairs": 4,
        "observations": 2,
        "readback": "verified",
    }
    if partial:
        result.update(
            format=staging.PARTIAL_TERMINAL_FORMAT,
            state_counts={
                "success": 1,
                "normal_null": 1,
                "failure": 0,
                "unattempted": 2,
            },
            receipt_date_coverage={"2026-10-11": {"monex": 1, "rakuten": 1}},
        )
    return result


def saved_terminal(p, value):
    store = MemoryR2Store()
    staging.save_new_plan(store, p)
    store.store_object(staging.prefix(p.run_id) + "/published.json", json.dumps(value).encode())
    store.put_keys.clear()
    store.get_keys.clear()
    store.head_keys.clear()
    return store


@pytest.mark.parametrize("partial", [False, True])
def test_valid_terminal_is_preserved_without_upgrading_or_reconstructing_counts(partial):
    p = plan()
    value = terminal(p, partial=partial)
    store = saved_terminal(p, value)
    assert staging.read_terminal(store, p) == value
    assert staging.terminal_status(value) == (
        "already_partially_published" if partial else "already_published"
    )
    assert len(value) == (9 if partial else 7)
    assert store.put_keys == []
    assert all(key.endswith("/published.json") for key in store.get_keys)


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_binding",
        "wrong_expected",
        "bool_expected",
        "bool_observations",
        "negative_observations",
        "excess_observations",
        "wrong_readback",
        "empty_release",
        "wrong_sha",
        "extra",
        "missing",
        "format_shape",
    ],
)
def test_full_terminal_exact_shape_and_binding_remain_strict(fault):
    p = plan()
    value = terminal(p)
    changes = {
        "wrong_binding": ("plan_sha256", "b" * 64),
        "wrong_expected": ("expected_pairs", 5),
        "bool_expected": ("expected_pairs", True),
        "bool_observations": ("observations", True),
        "negative_observations": ("observations", -1),
        "excess_observations": ("observations", 5),
        "wrong_readback": ("readback", "unknown"),
        "empty_release": ("release_id", ""),
        "wrong_sha": ("release_manifest_sha256", "A" * 64),
        "extra": ("state_counts", {}),
        "format_shape": ("format", []),
    }
    if fault == "missing":
        del value["observations"]
    else:
        key, changed = changes[fault]
        value[key] = changed
    with pytest.raises(ValueError, match="invalid external assessment published batch"):
        staging.read_terminal(saved_terminal(p, value), p)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_state",
        "extra_state",
        "bool_state",
        "negative_state",
        "state_total",
        "typed_total",
        "zero_typed",
        "coverage_total",
        "coverage_source_missing",
        "coverage_source_extra",
        "bool_coverage",
        "negative_coverage",
        "noncanonical_date",
        "invalid_date",
        "empty_date",
        "coverage_shape",
        "state_shape",
        "source_population",
    ],
)
def test_partial_terminal_rejects_invalid_state_or_received_date_coverage(fault):
    p = plan()
    value = terminal(p, partial=True)
    states, coverage = value["state_counts"], value["receipt_date_coverage"]
    counts = coverage["2026-10-11"]
    if fault == "missing_state":
        del states["unattempted"]
    elif fault == "extra_state":
        states["extra"] = 0
    elif fault == "bool_state":
        states["success"] = True
    elif fault == "negative_state":
        states["failure"] = -1
    elif fault == "state_total":
        states["failure"] = 1
    elif fault == "typed_total":
        value["observations"] = 3
    elif fault == "zero_typed":
        states.update(success=0, normal_null=0, unattempted=4)
        value["observations"] = 0
        value["receipt_date_coverage"] = {}
    elif fault == "coverage_total":
        counts["monex"] = 2
    elif fault == "coverage_source_missing":
        del counts["rakuten"]
    elif fault == "coverage_source_extra":
        counts["extra"] = 0
    elif fault == "bool_coverage":
        counts["monex"] = True
    elif fault == "negative_coverage":
        counts["monex"] = -1
    elif fault == "noncanonical_date":
        coverage["20261011"] = coverage.pop("2026-10-11")
    elif fault == "invalid_date":
        coverage["2026-02-30"] = coverage.pop("2026-10-11")
    elif fault == "empty_date":
        coverage["2026-10-12"] = {"monex": 0, "rakuten": 0}
    elif fault == "coverage_shape":
        value["receipt_date_coverage"] = []
    elif fault == "state_shape":
        value["state_counts"] = []
    else:
        states.update(success=2, normal_null=1, unattempted=1)
        value["observations"] = 3
        counts.update(monex=3, rakuten=0)
    with pytest.raises(ValueError, match="invalid external assessment published batch"):
        staging.read_terminal(saved_terminal(p, value), p)


def test_terminal_rejects_duplicate_top_level_and_nested_json_fields():
    p = plan()
    original = terminal(p, partial=True)
    store = saved_terminal(p, original)
    for field, duplicate in (
        ("observations", '"observations":2,'),
        ("state_counts", '"success":1,'),
    ):
        encoded = json.dumps(original)
        needle = '"observations":' if field == "observations" else '"success":'
        encoded = encoded.replace(needle, duplicate + needle, 1)
        store.store_object(staging.prefix(p.run_id) + "/published.json", encoded.encode())
        with pytest.raises(ValueError, match="invalid external assessment published batch"):
            staging.read_terminal(store, p)


@pytest.mark.parametrize("old_binding", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_historical_terminal_cli_reads_no_checkpoint_lease_db_or_l1(
    monkeypatch, capsys, partial, old_binding
):
    p = plan()
    if old_binding:
        p = replace(p, collector_binding="previous-collector")
    value = terminal(p, partial=partial)
    store = saved_terminal(p, value)
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: store)

    def forbidden(*args, **kwargs):
        pytest.fail("history CLI cannot acquire, refreeze, or read stages/L1")

    monkeypatch.setattr(daily, "read_stages", forbidden)
    monkeypatch.setattr(daily, "initialize", forbidden)
    monkeypatch.setattr(daily, "acquire_cohort", forbidden)
    assert daily.main(["terminal", "--run-id", p.run_id]) == 0
    assert json.loads(capsys.readouterr().out) == staging.terminal_result(value)
    assert staging.read_terminal(store, p) == value
    assert store.put_keys == []
    allowed = {staging.prefix(p.run_id) + suffix for suffix in ("/plan.json", "/published.json")}
    assert set(store.get_keys) <= allowed
    assert set(store.head_keys) <= allowed


def test_explicit_partial_requires_saved_plan_before_cache_or_l1_access(tmp_path):
    store = MemoryR2Store()
    cache = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="requires an existing fixed plan"):
        daily.initialize(
            store,
            run_id="missing",
            master_asof=date(2026, 10, 9),
            mirror=cache,
            require_existing=True,
        )
    assert not cache.exists()
    assert store.put_keys == store.get_keys == []
    assert store.head_keys == [staging.prefix("missing") + "/plan.json"]
