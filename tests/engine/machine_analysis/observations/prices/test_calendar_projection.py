"""Retain provider calendar categories and project cash-equity sessions on read."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pyarrow.parquet as pq
import pytest

from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_market_calendar
from baibai_engine.machine_analysis.observations.prices.calendar import JQuantsMarketCalendarDay
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    market_calendar_business_day,
    observation_calendar_context,
    read_price_calendar,
)
from baibai_engine.machine_analysis.observations.prices.calendar_reader import (
    read_market_calendar,
)
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
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


@pytest.mark.parametrize(
    ("source_date", "division", "day"),
    [
        ("2026-04-24", "1", date(2026, 4, 24)),
        ("2026-04-24T00:00:00", "1", date(2026, 4, 24)),
        ("2026-12-30", "2", date(2026, 12, 30)),
    ],
)
def test_calendar_reads_native_timestamp_and_half_day(tmp_path, source_date, division, day):
    database = tmp_path / "market.sqlite"
    store_jquants_market_calendar(
        database,
        [{"Date": source_date, "HolDiv": division}],
        requested_start=day,
        requested_end=day,
    )
    assert read_market_calendar(database, day, day) == [
        JQuantsMarketCalendarDay(day=day, is_business_day=True)
    ]


def test_unknown_calendar_category_is_not_treated_as_a_closed_day(tmp_path):
    start, unknown, end = date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)
    database = tmp_path / "market.sqlite"
    store_jquants_market_calendar(
        database,
        [
            {"Date": start.isoformat(), "HolDiv": "1"},
            {"Date": unknown.isoformat(), "HolDiv": "9"},
            {"Date": end.isoformat(), "HolDiv": "0"},
        ],
        requested_start=start,
        requested_end=end,
    )
    assert market_calendar_business_day(database, unknown) is None
    assert read_market_calendar(database, start, end) is None
    with closing(sqlite3.connect(database)) as connection:
        calendar = read_price_calendar(connection, as_of=end)
        assert unknown not in calendar
        assert price_calendar(calendar, as_of=end).expected_price_date is None
        with pytest.raises(ValueError, match="Exact-date"):
            observation_calendar_context(connection, unknown)
        with pytest.raises(ValueError, match="Continuous"):
            observation_calendar_context(connection, end)
