"""Requested date ranges bound replacement writes, not just coverage labels."""

import sqlite3
from contextlib import closing
from datetime import date

import pytest

from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_daily_bars,
    store_jquants_market_calendar,
)


@pytest.mark.parametrize("kind", ["bars", "calendar"])
@pytest.mark.parametrize("bad_date", ["2026-10-12", "2026-10-16", "2026-10-99"])
def test_range_ingest_does_not_overwrite_outside_dates_or_accept_invalid_dates(
    tmp_path, kind, bad_date
):
    path = tmp_path / "market.sqlite"
    writer = store_jquants_daily_bars if kind == "bars" else store_jquants_market_calendar

    def record(day, value):
        return (
            {"Date": day, "Code": "12340", "C": value}
            if kind == "bars"
            else {"Date": day, "HolDiv": str(value)}
        )

    writer(
        path,
        [record("2026-10-12", 1), record("2026-10-14", 1), record("2026-10-16", 1)],
        requested_start=date(2026, 10, 12),
        requested_end=date(2026, 10, 16),
    )
    before = path.read_bytes()
    message = "day" if bad_date == "2026-10-99" else "outside the requested range"
    with pytest.raises(ValueError, match=message):
        writer(
            path,
            [record("2026-10-14", 2), record(bad_date, 2)],
            requested_start=date(2026, 10, 13),
            requested_end=date(2026, 10, 15),
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("writer", [store_jquants_daily_bars, store_jquants_market_calendar])
def test_reversed_range_fails_before_creating_a_store(tmp_path, writer):
    path = tmp_path / "market.sqlite"
    with pytest.raises(ValueError, match="start must not follow end"):
        writer(path, [], requested_start=date(2026, 10, 15), requested_end=date(2026, 10, 13))
    assert not path.exists()


@pytest.mark.parametrize("kind", ["bars", "calendar"])
def test_valid_sparse_range_keeps_native_null_or_category(tmp_path, kind):
    path = tmp_path / "market.sqlite"
    writer = store_jquants_daily_bars if kind == "bars" else store_jquants_market_calendar
    record = (
        {"Date": "2026-10-14", "Code": "12340", "C": None}
        if kind == "bars"
        else {"Date": "2026-10-14", "HolDiv": "9"}
    )
    assert (
        writer(path, [record], requested_start=date(2026, 10, 13), requested_end=date(2026, 10, 15))
        == 1
    )
    with closing(sqlite3.connect(path)) as connection:
        query = (
            "SELECT close FROM jquants_daily_bars"
            if kind == "bars"
            else "SELECT holiday_division FROM jquants_market_calendar"
        )
        assert connection.execute(query).fetchone()[0] == (None if kind == "bars" else "9")
