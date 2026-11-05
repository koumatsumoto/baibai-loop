"""The all-issues originals preserve order, equality and full source-day scope."""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_interest_native import AT, DAY, attempt, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    PREFIX,
    MarginInterestL1,
)


def test_equal_pagination_no_body_copy_and_corrected_empty_removes_subset(tmp_path):
    store = MemoryR2Store()
    owner = MarginInterestL1(store, tmp_path)
    owner.initialize()
    original = attempt("original")
    owner.commit([original])
    fixed = owner.fixed()
    page = replace(original.pages[0], received_at=AT + timedelta(hours=1))
    equal = replace(
        original,
        request_id="equal",
        attempted_at=page.received_at,
        pages=(
            replace(page, body=body([], cursor="next")),
            replace(page, query={**page.query, "pagination_key": "next"}),
        ),
    )
    assert owner.commit([equal])["equal"] == ["equal"]
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == 1
    changed = attempt(
        "unknown-field", at=AT + timedelta(hours=2), rows=[{**records()[0], "Unknown": None}]
    )
    assert owner.commit([changed])["changed"] == ["unknown-field"]
    zero = attempt("zero", at=AT + timedelta(hours=3), rows=[{**records()[0], "Unknown": 0}])
    assert owner.commit([zero])["changed"] == ["zero"]
    assert owner.commit([attempt("empty", at=AT + timedelta(hours=4), rows=[])])["changed"] == [
        "empty"
    ]
    assert next(owner.fixed().requests(store, days=[DAY])).request.records() == []
    assert next(fixed.requests(store, days=[DAY])).request.records() == records()


def test_filtered_complete_request_cannot_establish_unfiltered_day(tmp_path):
    owner = MarginInterestL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    item = attempt("filtered")
    filtered = replace(
        item, pages=(replace(item.pages[0], query={"date": DAY.isoformat(), "code": "13010"}),)
    )
    with pytest.raises(ValueError, match="unfiltered"):
        owner.commit([filtered])
    assert not owner.fixed().value()["scopes"]


def test_count_tracks_latest_adopted_raw_records_and_failure_does_not_erase_it(tmp_path):
    owner = MarginInterestL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    owner.commit([attempt("duplicates", rows=records() + records())])
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["record_count"] == 2
    owner.commit([attempt("partial", at=AT + timedelta(hours=1), partial=True)])
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["record_count"] == 2
    assert owner.fixed().index().incomplete
    owner.commit([attempt("empty", at=AT + timedelta(hours=2), rows=[])])
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["record_count"] == 0


def test_same_clock_disagreement_and_later_recovery(tmp_path):
    owner = MarginInterestL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    owner.commit([attempt("a")])
    assert owner.commit([attempt("b", rows=[])])["held"] == ["b"]
    assert owner.fixed().index().incomplete
    owner.commit([attempt("c", at=AT + timedelta(hours=1))])
    assert not owner.fixed().index().incomplete
