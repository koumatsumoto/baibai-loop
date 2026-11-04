from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.machine_analysis.observations.financials.from_l1 import rebuild_financial_day

DAY = date(2026, 11, 2)


def receipt(identity: str, at: datetime, data: list[dict[str, object]]) -> RetainedRequest:
    page = JQuantsPage(
        "financial_summaries",
        {"date": "20261102"},
        at,
        json.dumps({"data": data}).encode(),
    )
    return RetainedRequest(identity, complete_request([page]), None)


def record(value: int) -> dict[str, object]:
    return {"Code": "13010", "DiscDate": "2026-11-02", "DiscNo": "a", "Sales": value}


def test_latest_native_receipt_rebuilds_without_old_l2() -> None:
    at = datetime(2026, 11, 2, tzinfo=UTC)
    old = receipt("old", at, [record(1)])
    latest = receipt("new", at + timedelta(hours=1), [record(2)])
    result = rebuild_financial_day([latest, old], day=DAY)
    assert result.request_id == "new"
    assert result.status == "ok"
    assert len(result.rows) == 1
    assert json.loads(result.rows[0][3])["Sales"] == 2


def test_empty_receipt_is_distinct_from_unavailable_and_incomplete() -> None:
    at = datetime(2026, 11, 2, tzinfo=UTC)
    incomplete = RetainedRequest("broken", None, "incomplete")
    result = rebuild_financial_day([receipt("empty", at, []), incomplete], day=DAY)
    assert result.request_id == "empty"
    assert result.rows == ()
    assert result.status == "ok"
    assert result.incomplete_requests == 1
    missing = rebuild_financial_day([incomplete], day=DAY)
    assert missing.request_id is None
    assert missing.status == "unavailable"
    assert missing.incomplete_requests == 1


def test_equal_time_conflict_does_not_pick_a_winner() -> None:
    at = datetime(2026, 11, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="ambiguous"):
        rebuild_financial_day(
            [receipt("a", at, [record(1)]), receipt("b", at, [record(2)])], day=DAY
        )


def test_rejects_rows_outside_the_native_request_date() -> None:
    at = datetime(2026, 11, 2, tzinfo=UTC)
    row = {**record(1), "DiscDate": "2026-11-01"}
    with pytest.raises(ValueError, match="outside"):
        rebuild_financial_day([receipt("a", at, [row])], day=DAY)


def test_old_tie_does_not_mask_a_later_unambiguous_receipt() -> None:
    at = datetime(2026, 11, 2, tzinfo=UTC)
    result = rebuild_financial_day(
        [
            receipt("a", at, [record(1)]),
            receipt("b", at, [record(2)]),
            receipt("c", at + timedelta(hours=1), [record(3)]),
        ],
        day=DAY,
    )
    assert result.request_id == "c"
    assert json.loads(result.rows[0][3])["Sales"] == 3


def test_duckdb_materialization_uses_only_the_rebuilt_day() -> None:
    import duckdb

    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        materialize_financial_days,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    result = rebuild_financial_day([receipt("new", at, [record(2)])], day=DAY)
    with duckdb.connect(":memory:") as connection:
        materialize_financial_days(connection, [result], l1_coverage_status="partial")
        rows = connection.execute("SELECT * FROM financial_summaries").fetchall()
    assert rows == list(result.rows)


def test_incomplete_source_remains_visible_in_status() -> None:
    import duckdb

    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        materialize_financial_days,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    result = rebuild_financial_day(
        [receipt("new", at, [record(2)]), RetainedRequest("broken", None, "partial")], day=DAY
    )
    with duckdb.connect(":memory:") as connection:
        materialize_financial_days(connection, [result], l1_coverage_status="partial")
        assert connection.execute(
            "SELECT incomplete_requests FROM financial_receipt_status"
        ).fetchone() == (1,)


def test_incomplete_request_for_another_day_does_not_block_this_day() -> None:
    import duckdb

    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        materialize_financial_days,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    other = RetainedRequest("partial", None, "incomplete", {"date": "20261101"})
    result = rebuild_financial_day([receipt("new", at, [record(2)]), other], day=DAY)
    assert result.incomplete_requests == 0
    with duckdb.connect(":memory:") as connection:
        materialize_financial_days(connection, [result], l1_coverage_status="partial")
        assert connection.execute("SELECT count(*) FROM financial_summaries").fetchone() == (1,)


def test_rejected_native_rows_remain_visible_as_partial() -> None:
    import duckdb

    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        materialize_financial_days,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    result = rebuild_financial_day(
        [receipt("bad", at, [{"Code": "", "DiscDate": "2026-11-02"}])], day=DAY
    )
    assert result.status == "partial"
    assert result.rejected_count == 1
    with duckdb.connect(":memory:") as connection:
        materialize_financial_days(connection, [result], l1_coverage_status="partial")
        assert connection.execute(
            "SELECT projection_status, rejected_count FROM financial_receipt_status"
        ).fetchone() == ("partial", 1)


def test_duckdb_rebuild_matches_current_projection_for_same_receipt(tmp_path) -> None:
    import sqlite3

    import duckdb

    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        materialize_financial_days,
    )
    from baibai_engine.machine_analysis.observations.jquants_writer import (
        store_jquants_fin_summaries,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    native = [record(2), {"Code": "99990", "DiscDate": "2026-11-02", "DiscNo": "b"}]
    result = rebuild_financial_day([receipt("full", at, native)], day=DAY)
    path = tmp_path / "existing.sqlite"
    store_jquants_fin_summaries(path, native, requested_start=DAY, requested_end=DAY)
    with sqlite3.connect(path) as old:
        expected = set(
            old.execute(
                "SELECT ticker, disclosed_at, disclosure_number, document_payload FROM jquants_fin_summaries"
            )
        )
    with duckdb.connect(":memory:") as new:
        materialize_financial_days(new, [result], l1_coverage_status="partial")
        actual = set(new.execute("SELECT * FROM financial_summaries").fetchall())
    assert actual == expected


def test_multiple_days_are_built_in_one_pass_and_missing_is_not_empty() -> None:
    from baibai_engine.machine_analysis.observations.financials.from_l1 import (
        rebuild_financial_days,
    )

    at = datetime(2026, 11, 2, tzinfo=UTC)
    other = date(2026, 11, 3)
    requests = iter(
        [
            receipt("new", at, [record(2)]),
            RetainedRequest("broken", None, "partial", {"date": "20261103"}),
        ]
    )
    result = rebuild_financial_days(requests, days=[DAY, other])
    assert result[DAY].request_id == "new"
    assert result[DAY].incomplete_requests == 0
    assert result[other].status == "unavailable"
    assert result[other].incomplete_requests == 1
