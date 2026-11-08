"""Actual range originals, per-day adoption and fixed replay without any L2."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    HEAD_KEY,
    PREFIX,
    CalendarAttempt,
    CalendarL1,
    interval,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.native_index import calendar_sessions

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, tzinfo=UTC)


def attempt(identity, start=DAY, end=None, *, hour=0, rows=None, partial=False, query=None):
    end = end or start
    if rows is None:
        rows = [{"Date": day.isoformat(), "HolDiv": "1"} for day in interval(start, end)]
    envelope = {"data": rows, "unused": {"n": None, "z": 0, "f": False}}
    if partial:
        envelope["pagination_key"] = "more"
    page = JQuantsPage(
        "market_calendar",
        query or {"from": start.isoformat(), "to": end.isoformat()},
        AT + timedelta(hours=hour),
        json.dumps(envelope).encode(),
    )
    return CalendarAttempt(identity, start, end, None, (page,))


def owner(tmp_path):
    store = MemoryR2Store()
    result = CalendarL1(store, tmp_path)
    result.initialize()
    return store, result


def test_overlap_and_equal_reconfirmation_use_real_order_without_retiming_pages(tmp_path):
    store, source = owner(tmp_path)
    d2, d3 = DAY + timedelta(days=1), DAY + timedelta(days=2)
    first = attempt("A1", end=d2)
    source.commit([first])
    frozen = source.fixed()
    source.commit(
        [
            attempt(
                "B2",
                d2,
                d3,
                hour=1,
                rows=[
                    {"Date": d2.isoformat(), "HolDiv": "0"},
                    {"Date": d3.isoformat(), "HolDiv": "1"},
                ],
            )
        ]
    )
    assert source.fixed().value()["days"][d2.isoformat()]["division"] == "0"
    assert source.commit([attempt("A3", end=d2, hour=2)])["changed"] == ["A3"]
    fixed = source.fixed()
    assert fixed.value()["days"][d2.isoformat()]["adopted"] == "A3"
    before = len([key for key in store.values if key.startswith(PREFIX + "packs/")])
    assert source.commit([attempt("A4", end=d2, hour=3)])["equal"] == ["A4"]
    assert len([key for key in store.values if key.startswith(PREFIX + "packs/")]) == before
    state = source.fixed().value()["days"][d2.isoformat()]
    assert state["confirmed_at"] == (AT + timedelta(hours=3)).isoformat()
    assert {item.request_id: item.order_time for item in source.fixed().requests(store)}[
        "A3"
    ] == AT + timedelta(hours=2)
    assert next(frozen.requests(store)).value() == first.value()
    del store.values[HEAD_KEY]
    assert next(frozen.requests(store)).value() == first.value()


def test_partial_overlap_recovery_does_not_recontaminate_resolved_days(tmp_path):
    store, source = owner(tmp_path)
    d2, d3 = DAY + timedelta(days=1), DAY + timedelta(days=2)
    source.commit([attempt("A1", end=d2)])
    source.commit([attempt("B2", d2, d3, hour=1, partial=True)])
    assert source.fixed().plan(start=DAY, end=d3) == (d2, d3)
    assert source.commit([attempt("A3", end=d2, hour=2)])["equal"] == ["A3"]
    assert source.fixed().plan(start=DAY, end=d3) == (d3,)
    sessions, unknown = calendar_sessions(source.fixed().index(), start=DAY, end=d3)
    assert sessions == {DAY: True, d2: True, d3: None}
    assert not unknown
    assert {item.request_id for item in source.fixed().requests(store)} == {"A1", "B2"}


@pytest.mark.parametrize(
    ("rows", "missing"),
    [
        ([], True),
        ([{"Date": DAY.isoformat(), "HolDiv": ""}], True),
        ([{"Date": DAY.isoformat(), "HolDiv": None}], True),
        ([{"Date": DAY.isoformat(), "HolDiv": "0"}] * 2, True),
        ([{"Date": DAY.isoformat(), "HolDiv": "1"}, {"Date": "bad", "HolDiv": "1"}], True),
        ([{"Date": DAY.isoformat(), "HolDiv": "1"}, {"Date": "2020-01-01", "HolDiv": "1"}], True),
        ([{"Date": DAY.isoformat(), "HolDiv": "future-code", "extra": [None, 0, False]}], False),
    ],
)
def test_raw_completion_is_not_a_proven_closed_session(tmp_path, rows, missing):
    store, source = owner(tmp_path)
    raw = attempt("raw", rows=rows)
    source.commit([raw])
    assert bool(source.fixed().plan(start=DAY, end=DAY)) is missing
    assert next(source.fixed().requests(store)).value() == raw.value()
    sessions, _ = calendar_sessions(source.fixed().index(), start=DAY, end=DAY)
    assert sessions[DAY] is None


def test_scope_query_form_order_unknown_fields_and_older_receipt_are_kept(tmp_path):
    store, source = owner(tmp_path)
    initial = attempt("initial")
    source.commit([initial])
    assert source.commit(
        [
            attempt(
                "same", hour=1, query={"from": DAY.strftime("%Y%m%d"), "to": DAY.strftime("%Y%m%d")}
            )
        ]
    )["equal"] == ["same"]
    changed = attempt(
        "unknown-field", hour=2, rows=[{"Date": DAY.isoformat(), "HolDiv": "1", "extra": "retain"}]
    )
    source.commit([changed])
    old = attempt("old", hour=1, rows=[])
    assert source.commit([old])["held"] == ["old"]
    assert source.fixed().value()["days"][DAY.isoformat()]["adopted"] == "unknown-field"
    tied = attempt("tied", hour=2, rows=[])
    source.commit([tied])
    assert source.fixed().plan(start=DAY, end=DAY) == (DAY,)
    assert source.commit([tied])["retried"] == ["tied"]
    with pytest.raises(ValueError, match="identity conflicts"):
        source.commit([attempt("tied", hour=2)])
    assert (
        next(x for x in source.fixed().requests(store) if x.request_id == "unknown-field").value()
        == changed.value()
    )


def test_missing_day_response_replaces_only_its_own_range(tmp_path):
    _store, source = owner(tmp_path)
    d2, d3 = DAY + timedelta(days=1), DAY + timedelta(days=2)
    source.commit([attempt("old", end=d3)])
    source.commit(
        [attempt("correction", end=d2, hour=1, rows=[{"Date": DAY.isoformat(), "HolDiv": "0"}])]
    )
    assert source.fixed().plan(start=DAY, end=d3) == (d2,)
    assert source.fixed().value()["days"][d3.isoformat()]["adopted"] == "old"


@pytest.mark.parametrize(
    "body",
    [
        b'{"data":[{"Date":"2026-11-05","HolDiv":"1"}],"extra":[1e309]}',
        b'{"data":[{"Date":"2026-11-05","HolDiv":"1"}],"extra":["\\ud800"]}',
    ],
)
def test_invalid_extra_envelope_remains_raw_and_unavailable(tmp_path, body):
    store, source = owner(tmp_path)
    original = attempt("bad")
    original = replace(
        original,
        pages=(
            replace(
                original.pages[0],
                body=body,
            ),
        ),
    )
    assert source.commit([original])["failed"] == ["bad"]
    fixed = CalendarL1(store, tmp_path / "fresh-reader").fixed()
    retained = {item.request_id: item for item in fixed.requests(store)}
    assert retained["bad"].value() == original.value()
    assert fixed.plan(start=DAY, end=DAY) == (DAY,)
    assert calendar_sessions(fixed.index(), start=DAY, end=DAY)[0][DAY] is None


def test_same_clock_identical_content_needs_no_ambiguity_or_extra_body(tmp_path):
    store, source = owner(tmp_path)
    source.commit([attempt("first")])
    packs = {key for key in store.values if key.startswith(PREFIX + "packs/")}
    assert source.commit([attempt("same-clock")])["equal"] == ["same-clock"]
    assert source.fixed().plan(start=DAY, end=DAY) == ()
    assert {key for key in store.values if key.startswith(PREFIX + "packs/")} == packs
    source.commit([attempt("conflict", rows=[])])
    assert source.fixed().plan(start=DAY, end=DAY) == (DAY,)
    source.commit([attempt("not-a-resolution")])
    assert source.fixed().plan(start=DAY, end=DAY) == (DAY,)
