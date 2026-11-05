"""Disclosure-day acquisition keeps source order, repetitions and raw source fields."""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import AT, DAY, attempt, records

from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    PREFIX,
    ShortSaleReportsL1,
    plan_short_sale_days,
)


def test_first_plan_is_eight_inclusive_calendar_days_without_calendar_or_l2(tmp_path):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    assert plan_short_sale_days(owner.fixed(), asof=DAY) == tuple(
        DAY - timedelta(days=i) for i in range(7, -1, -1)
    )
    with pytest.raises(ValueError, match="interval"):
        plan_short_sale_days(owner.fixed(), asof=DAY, start=DAY + timedelta(days=1))


def test_empty_history_anchor_gaps_failure_and_same_clock_remain_in_plan(tmp_path):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path)
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
    planned = plan_short_sale_days(owner.fixed(), asof=DAY)
    assert first not in planned
    assert first + timedelta(days=1) in planned
    assert first + timedelta(days=2) in planned
    assert first + timedelta(days=3) in planned
    assert DAY - timedelta(days=7) in planned
    assert DAY in planned


def test_equal_pagination_no_body_copy_and_corrected_empty_replaces_disclosure_day(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path)
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


@pytest.mark.parametrize(
    "query",
    [
        {"disc_date": DAY.isoformat(), "code": "13010"},
        {"date": DAY.isoformat()},
        {"calc_date": DAY.isoformat()},
        {"disc_date_from": DAY.isoformat(), "disc_date_to": DAY.isoformat()},
    ],
)
def test_filtered_or_wrong_date_query_cannot_establish_full_disclosure_day(tmp_path, query):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    item = attempt("filtered")
    filtered = replace(item, pages=(replace(item.pages[0], query=query),))
    with pytest.raises(ValueError, match="unfiltered"):
        owner.commit([filtered])
    assert not owner.fixed().value()["scopes"]


def test_raw_repeated_rows_and_order_are_part_of_the_source_identity(tmp_path):
    owner = ShortSaleReportsL1(MemoryR2Store(), tmp_path)
    owner.initialize()
    first = records()[0]
    second = {**first, "ShrtPosToSO": 0.008, "Unknown": {"PubDate": "not-an-alias"}}
    original = attempt("original", rows=[first, first, second])
    owner.commit([original])
    fixed = owner.fixed()
    changed = attempt("reordered", at=AT + timedelta(hours=1), rows=[second, first, first])
    assert owner.commit([changed])["changed"] == ["reordered"]
    assert next(fixed.requests(owner.store, days=[DAY])).request.records() == [first, first, second]
    assert next(owner.fixed().requests(owner.store, days=[DAY])).request.records() == [
        second,
        first,
        first,
    ]
