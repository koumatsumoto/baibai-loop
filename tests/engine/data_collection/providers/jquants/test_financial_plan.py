from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.data_collection.providers.jquants.financial_plan import (
    plan_financial_acquisition,
)

START = date(2026, 11, 1)
END = date(2026, 11, 4)
AT = datetime(2026, 11, 4, tzinfo=UTC)


def complete(
    identity: str, day: str, when: datetime, records: list[dict] | None = None
) -> RetainedRequest:
    request = complete_request(
        [
            JQuantsPage(
                "financial_summaries",
                {"date": day},
                when,
                json.dumps({"data": records or []}).encode(),
            )
        ]
    )
    return RetainedRequest(identity, request, None, {"date": day}, when)


def failed(identity: str, day: str, when: datetime | None) -> RetainedRequest:
    return RetainedRequest(identity, None, "incomplete", {"date": day}, when)


def test_complete_empty_receipts_count_and_refresh_tail_is_explicit() -> None:
    reqs = [
        complete("a", "20261101", AT),
        complete("b", "20261102", AT),
        complete("d", "20261104", AT),
    ]
    plan = plan_financial_acquisition(reqs, start=START, end=END, revision_overlap_days=0)
    assert plan.days == (date(2026, 11, 3), date(2026, 11, 4))
    assert plan.unknown_incomplete_requests == ()


def test_later_incomplete_is_retried_but_older_incomplete_is_superseded() -> None:
    reqs = [
        complete("a", "20261101", AT),
        failed("later", "20261101", AT + timedelta(seconds=1)),
        failed("old", "20261102", AT - timedelta(seconds=1)),
        complete("b", "20261102", AT),
        complete("c", "20261103", AT),
    ]
    plan = plan_financial_acquisition(reqs, start=START, end=END, revision_overlap_days=0)
    assert plan.days == (date(2026, 11, 1), date(2026, 11, 4))


def test_unknown_failure_does_not_become_coverage() -> None:
    reqs = [
        complete("a", "20261101", AT),
        failed("untimed", "20261101", None),
        RetainedRequest("unknown", None, "bad query"),
    ]
    plan = plan_financial_acquisition(reqs, start=START, end=END, revision_overlap_days=0)
    assert date(2026, 11, 1) in plan.days
    assert plan.unknown_incomplete_requests == ("unknown",)


def test_filtered_request_does_not_satisfy_full_day() -> None:
    page = JQuantsPage(
        "financial_summaries", {"date": "20261101", "code": "1301"}, AT, b'{"data": []}'
    )
    req = RetainedRequest("single", complete_request([page]), None)
    plan = plan_financial_acquisition([req], start=START, end=END, revision_overlap_days=0)
    assert plan.days == tuple(START + timedelta(days=i) for i in range(4))


def test_invalid_ranges_are_rejected() -> None:
    with pytest.raises(ValueError, match="start"):
        plan_financial_acquisition([], start=END, end=START, revision_overlap_days=0)
    with pytest.raises(ValueError, match="overlap"):
        plan_financial_acquisition([], start=START, end=END, revision_overlap_days=-1)


def test_shared_request_identity_must_have_identical_original_pages() -> None:
    import gzip

    from baibai_engine.data_collection.providers.jquants.retained import decode_retained_rows

    query = json.dumps({"date": "20261101"})
    clock = "2026-11-03T00:00:00.000000+00:00"
    first = (
        "same",
        0,
        "financial_summaries",
        query,
        clock,
        gzip.compress(b'{"data": []}', mtime=0),
    )
    conflict = (
        "same",
        0,
        "financial_summaries",
        query,
        clock,
        gzip.compress(b'{"data": [{"x": 1}]}', mtime=0),
    )
    a = decode_retained_rows("same", [first])
    b = decode_retained_rows("same", [conflict])
    plan = plan_financial_acquisition([a, a], start=START, end=END, revision_overlap_days=0)
    assert date(2026, 11, 1) not in plan.days
    with pytest.raises(ValueError, match="conflicting retained page"):
        plan_financial_acquisition([a, b], start=START, end=END, revision_overlap_days=0)


def test_equal_timestamp_distinct_receipts_are_not_silently_chosen() -> None:
    first = complete("a", "20261101", AT, [{"Code": "13010"}])
    second = complete("b", "20261101", AT, [{"Code": "13020"}])
    plan = plan_financial_acquisition(
        [first, second], start=START, end=END, revision_overlap_days=0
    )
    assert START in plan.days


def test_native_plan_matches_existing_coverage_for_full_and_empty_days(tmp_path) -> None:
    from contextlib import closing

    from baibai_engine.storage.sqlite.source_cache.coverage import (
        missing_intervals,
        record_source_coverage,
    )
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection

    requests = [complete("a", "20261101", AT), complete("b", "20261102", AT, [{"Code": "13010"}])]
    db = tmp_path / "previous.sqlite"
    with closing(open_connection(db)) as conn, conn:
        for day, count in [(date(2026, 11, 1), 0), (date(2026, 11, 2), 1)]:
            record_source_coverage(
                conn,
                source="jquants_fin_summaries",
                coverage_key=day.isoformat(),
                coverage_start=day.isoformat(),
                coverage_end=day.isoformat(),
                record_count=count,
                status="ok",
                error=None,
            )
        missing = missing_intervals(conn, "jquants_fin_summaries", START, END)
    previous = {
        first + timedelta(days=offset)
        for first, last in missing
        for offset in range((last - first).days + 1)
    } | {END}
    plan = plan_financial_acquisition(requests, start=START, end=END, revision_overlap_days=0)
    assert set(plan.days) == previous
