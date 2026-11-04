from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_daily_bars
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    materialize_price_days,
    rebuild_price_days,
)


def receipt(identity, *, records, at=None, body=None):
    query = {"date": "2026-11-02"}
    page = JQuantsPage(
        "daily_bars",
        query,
        at or datetime(2026, 11, 3, tzinfo=UTC),
        body or json.dumps({"data": records}).encode(),
    )
    return RetainedRequest(identity, complete_request([page]), None)


def test_price_rebuild_matches_existing_projection_without_previous_l2(tmp_path):
    records = [
        {
            "Code": "13010",
            "Date": "2026-11-02",
            "O": 100,
            "H": 120,
            "L": 90,
            "C": 110,
            "Vo": 500,
            "Va": 55000,
            "AdjFactor": 1,
        }
    ]
    day = date(2026, 11, 2)
    old = tmp_path / "old.sqlite"
    store_jquants_daily_bars(old, records, requested_start=day, requested_end=day)
    with sqlite3.connect(old) as conn:
        expected = conn.execute("SELECT * FROM jquants_daily_bars").fetchall()
    old.unlink()
    results = rebuild_price_days([receipt("source", records=records)], days=[day])
    with duckdb.connect(":memory:") as conn:
        materialize_price_days(conn, results.values(), l1_coverage_status="partial")
        assert conn.execute("SELECT * FROM daily_bars").fetchall() == expected
        assert conn.execute(
            "SELECT projection_status, l1_coverage_status FROM price_receipt_status"
        ).fetchone() == ("ok", "partial")


def test_price_missing_empty_and_later_complete():
    day = date(2026, 11, 2)
    now = datetime(2026, 11, 3, tzinfo=UTC)
    first = receipt("first", records=[], at=now)
    later = receipt("later", records=[], at=now + timedelta(seconds=1))
    values = rebuild_price_days([first, later], days=[day, day + timedelta(days=1)])
    assert values[day].request_id == "later"
    assert values[day].status == "ok"
    assert values[day + timedelta(days=1)].status == "unavailable"


def test_price_refuses_payload_outside_requested_day():
    day = date(2026, 11, 2)
    source = receipt("bad", records=[{"Code": "13010", "Date": "2026-11-03", "C": 100}])
    with pytest.raises(ValueError, match="outside the request"):
        rebuild_price_days([source], days=[day])


def test_price_materialization_rolls_back_on_duplicate_ticker_day():
    from baibai_engine.machine_analysis.observations.prices.from_l1 import PriceDay

    row = ("1301", "2026-11-02", None, None, None, 100.0, None, None, None, None, None, None, None)
    duplicate = PriceDay(date(2026, 11, 2), "id", (row, row), "ok", 0, 0, 0)
    with duckdb.connect(":memory:") as conn:
        with pytest.raises(duckdb.ConstraintException):
            materialize_price_days(conn, [duplicate], l1_coverage_status="partial")
        assert (
            conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='daily_bars'"
            ).fetchone()[0]
            == 0
        )


def test_materialize_price_days_appends_distinct_months_and_rolls_back_duplicates():
    from baibai_engine.machine_analysis.observations.prices.from_l1 import PriceDay

    first = PriceDay(
        date(2026, 10, 30),
        "first",
        (
            (
                "1301",
                "2026-10-30",
                None,
                None,
                None,
                100.0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        "ok",
        0,
        0,
        0,
    )
    second = PriceDay(
        date(2026, 11, 2),
        "second",
        (
            (
                "1301",
                "2026-11-02",
                None,
                None,
                None,
                110.0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        "ok",
        0,
        0,
        0,
    )
    with duckdb.connect(":memory:") as conn:
        materialize_price_days(conn, [first], l1_coverage_status="complete")
        materialize_price_days(conn, [second], l1_coverage_status="complete", append=True)
        with pytest.raises(duckdb.ConstraintException):
            materialize_price_days(conn, [second], l1_coverage_status="complete", append=True)
        assert conn.execute("SELECT count(*) FROM daily_bars").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM price_receipt_status").fetchone()[0] == 2
