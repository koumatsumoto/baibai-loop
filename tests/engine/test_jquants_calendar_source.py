"""Retain provider calendar categories and project cash-equity sessions on read."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pyarrow.parquet as pq

from baibai_engine.machine_analysis.observations.jquants_decode import normalize_market_calendar
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_market_calendar
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    market_calendar_business_day,
)
from baibai_engine.machine_analysis.observations.prices.calendar_reader import (
    read_market_calendar,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_MARKET_CALENDAR,
)
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.publication.datasets import LAKE_DATASETS


def test_calendar_categories_survive_sqlite_and_lake_without_becoming_booleans(tmp_path):
    start = date(2026, 1, 1)
    end = start + timedelta(days=3)
    records = [
        {"Date": (start + timedelta(days=i)).isoformat(), "HolDiv": str(i)} for i in range(4)
    ]
    database = tmp_path / "market.sqlite"
    assert (
        store_jquants_market_calendar(database, records, requested_start=start, requested_end=end)
        == 4
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT holiday_division FROM jquants_market_calendar ORDER BY day"
        ).fetchall() == [("0",), ("1",), ("2",), ("3",)]
    days = read_market_calendar(database, start, end)
    assert days is not None
    assert [day.is_business_day for day in days] == [False, True, True, False]
    assert [normalize_market_calendar(record).is_business_day for record in records] == [
        False,
        True,
        True,
        False,
    ]
    assert [
        market_calendar_business_day(database, start + timedelta(days=i)) for i in range(4)
    ] == [False, True, True, False]

    mirror = tmp_path / "lake"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.market_calendar"],
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="calendar-source",
        )
    obj = report.manifest.partitions[0].objects[0]
    rows = pq.ParquetFile(mirror / obj.key).read().to_pylist()
    assert JQUANTS_MARKET_CALENDAR.contract_version == 2
    assert rows == [
        {"day": record["Date"], "holiday_division": record["HolDiv"]} for record in records
    ]
