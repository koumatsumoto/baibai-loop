"""The designated subset keeps its publication-day acquisition semantics."""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_alerts_native import AT, DAY, attempt, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    PREFIX,
    MarginAlertsL1,
    plan_margin_alert_days,
)


def test_first_plan_is_eight_inclusive_calendar_days_without_calendar_or_l2(tmp_path):
    owner = MarginAlertsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    assert plan_margin_alert_days(owner.fixed(), asof=DAY) == tuple(
        DAY - timedelta(days=i) for i in range(7, -1, -1)
    )
    with pytest.raises(ValueError, match="interval"):
        plan_margin_alert_days(owner.fixed(), asof=DAY, start=DAY + timedelta(days=1))


def test_empty_history_anchor_gaps_failure_and_same_clock_remain_in_plan(tmp_path):
    owner = MarginAlertsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    first = DAY - timedelta(days=20)
    owner.commit([attempt("empty-anchor", day=first, rows=[])])
    owner.commit([attempt("old", day=first + timedelta(days=2))])
    owner.commit(
        [
            attempt(
                "partial", day=first + timedelta(days=2), at=AT + timedelta(hours=1), partial=True
            )
        ]
    )
    owner.commit([attempt("ambiguous-a", day=first + timedelta(days=3))])
    owner.commit([attempt("ambiguous-b", day=first + timedelta(days=3), rows=[])])
    planned = plan_margin_alert_days(owner.fixed(), asof=DAY)
    assert first not in planned
    assert first + timedelta(days=1) in planned
    assert first + timedelta(days=2) in planned
    assert first + timedelta(days=3) in planned
    assert DAY - timedelta(days=7) in planned
    assert DAY in planned


def test_equal_pagination_no_body_copy_and_corrected_empty_removes_subset(tmp_path):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path)
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
    owner = MarginAlertsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    item = attempt("filtered")
    filtered = replace(
        item, pages=(replace(item.pages[0], query={"date": DAY.isoformat(), "code": "13010"}),)
    )
    with pytest.raises(ValueError, match="unfiltered"):
        owner.commit([filtered])
    assert not owner.fixed().value()["scopes"]
