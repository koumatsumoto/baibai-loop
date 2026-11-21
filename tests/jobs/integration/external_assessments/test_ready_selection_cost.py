"""累積集合の比較を減らしても、包含・時計・拒否は変えない。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import permutations
from uuid import UUID

import pytest

import baibai_jobs.integration.external_assessment_ready as ready
from baibai_jobs.acquisition.external_assessments.cohorts import (
    PairEntry,
    make_plan,
    new_result,
)
from baibai_jobs.acquisition.external_assessments.staging import CohortStage


def prepared(*indexes, cumulative=True):
    plan = make_plan(
        run_id="synthetic-selection",
        sources=("monex",),
        tickers=("1000", "1001", "1002"),
        cohort_size=1,
        universe_binding="synthetic-universe",
        collector_binding="synthetic-collector",
    )
    stages = {}
    for index in indexes:
        result = new_result(plan, index)
        entry = PairEntry(
            "monex",
            plan.cohorts[index].tickers[0],
            "failure",
            "request_timeout",
            attempt_id=str(UUID(int=index + 1)),
        )
        stages[index] = (CohortStage(replace(result, entries=(entry,))), "synthetic-etag")
    return ready.prepare_saved_stages(plan, stages, cumulative=cumulative)


def entry(item, clock=0, name="synthetic-ready"):
    return datetime(2026, 11, 16, tzinfo=UTC) + timedelta(seconds=clock), {"key": name}, item


def previous_select(entries):
    supersets = [
        item
        for item in entries
        if item[2].cumulative and all(ready._contains(item[2], old[2]) for old in entries)
    ]
    if not supersets:
        raise ValueError("external cumulative ready sets are incomparable")
    return max(supersets, key=lambda item: item[0])


@pytest.mark.parametrize(
    "case",
    ["chain", "late_subset", "siblings", "incomparable", "mixed", "ties", "equivalent_clocks"],
)
def test_every_small_listing_order_matches_previous_selection(case):
    small, other, full = prepared(0), prepared(1), prepared(0, 1, 2)
    values = {
        "chain": [entry(small), entry(prepared(0, 1), 1), entry(full, 2)],
        "late_subset": [entry(small, 20), entry(prepared(0, 1), 10), entry(full, 0)],
        "siblings": [entry(small, 20), entry(other, 30), entry(full, 0)],
        "incomparable": [entry(small), entry(other, 1)],
        "mixed": [entry(full, 100), entry(replace(full, cumulative=False), 200), entry(small)],
        "ties": [entry(full, 1, "first"), entry(full, 1, "second"), entry(small, 10)],
        "equivalent_clocks": [entry(full, 2, "new"), entry(full, 1, "old"), entry(small, 10)],
    }[case]
    for ordered in permutations(values):
        try:
            expected = previous_select(ordered)
        except ValueError:
            with pytest.raises(ValueError, match="cumulative ready sets are incomparable"):
                ready._select_cumulative(list(ordered))
        else:
            assert ready._select_cumulative(list(ordered)) is expected


def test_same_cohort_extension_not_just_stage_count_or_latest_clock():
    old = prepared(0)
    stage, etag = old.stages[0]
    newer = replace(old, stages={0: (replace(stage, http_requests=2), etag)})
    latest_small = entry(old, 20)
    true_larger = entry(newer, 0)
    assert ready._select_cumulative([latest_small, true_larger]) is true_larger
    assert ready._select_cumulative([true_larger, latest_small]) is true_larger


@pytest.mark.parametrize("change", ["outcome", "raw_prefix", "counters"])
def test_same_size_conflicts_still_refuse(change):
    old = prepared(0)
    stage, etag = old.stages[0]
    stage = replace(stage, http_requests=1, response_content_bytes=2, raw_bundle_keys=("one",))
    old = replace(old, stages={0: (stage, etag)})
    if change == "outcome":
        result = replace(
            stage.result, entries=(replace(stage.result.entries[0], attempt_id=str(UUID(int=9))),)
        )
        changed = replace(stage, result=result)
    elif change == "raw_prefix":
        changed = replace(stage, raw_bundle_keys=("other",))
    else:
        changed = replace(stage, http_requests=2, response_content_bytes=1)
    other = replace(old, stages={0: (changed, etag)})
    for ordered in permutations([entry(old), entry(other, 1)]):
        with pytest.raises(ValueError, match="incomparable"):
            ready._select_cumulative(list(ordered))


def test_cumulative_candidate_must_cover_non_cumulative_entry():
    entries = [entry(prepared(0)), entry(replace(prepared(1), cumulative=False))]
    with pytest.raises(ValueError, match="incomparable"):
        ready._select_cumulative(entries)


def test_ascending_retained_extensions_need_linear_comparisons(monkeypatch):
    base = prepared(0)
    stage, etag = base.stages[0]
    entries = [
        entry(replace(base, stages={0: (replace(stage, http_requests=index), etag)}), index)
        for index in range(100)
    ]
    contains = ready._contains
    calls = 0

    def counted(new, old):
        nonlocal calls
        calls += 1
        return contains(new, old)

    monkeypatch.setattr(ready, "_contains", counted)
    assert ready._select_cumulative(entries) is entries[-1]
    assert calls <= 3 * len(entries)
