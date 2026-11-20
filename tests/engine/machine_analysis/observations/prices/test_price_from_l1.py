from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_daily_bars
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    price_bars_to_arrow,
    price_receipts_to_arrow,
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
    bars = price_bars_to_arrow(results.values())
    assert [tuple(row.values()) for row in bars.to_pylist()] == expected
    status = price_receipts_to_arrow([results[day].receipt_row()], l1_coverage_status="partial")
    assert status.select(["projection_status", "l1_coverage_status"]).to_pylist() == [
        {"projection_status": "ok", "l1_coverage_status": "partial"}
    ]


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


def test_price_rebuild_collapses_equal_rows_but_refuses_conflicting_ticker_day():
    day = date(2026, 11, 2)
    record = {"Code": "13010", "Date": day.isoformat(), "C": 100}
    equal = receipt("equal", records=[record, dict(record)])
    assert price_bars_to_arrow(rebuild_price_days([equal], days=[day]).values()).num_rows == 1
    conflicting = receipt("conflicting", records=[record, {**record, "C": 101}])
    with pytest.raises(JQuantsProviderError, match="conflicting daily bars"):
        rebuild_price_days([conflicting], days=[day])
