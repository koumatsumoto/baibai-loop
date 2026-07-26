from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
)
from baibai_engine.read_api.macro import MacroGranularity, macro_indicator_series


def test_macro_indicator_series_aggregates_each_period_to_its_last_observation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "macro.sqlite"
    start = date(2016, 1, 1)
    end = date(2025, 12, 31)
    observations: list[ObservationRecord] = []
    current = start
    while current <= end:
        observations.append(
            ObservationRecord(
                series_id="us.10y",
                observed_at=current,
                value=(current - start).days / 1000.0,
                unit="percent",
                source_url="https://example.com/us10y.csv",
                vintage_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        current += timedelta(days=1)
    connection = initialize_database(database)
    try:
        insert_observations(connection, observations)
        connection.commit()
    finally:
        connection.close()

    daily = _points(database, "daily", start=start, end=end)
    weekly = _points(database, "weekly", start=start, end=end)
    monthly = _points(database, "monthly", start=start, end=end)
    yearly = _points(database, "yearly", start=start, end=end)

    expected_weeks = {day.isocalendar()[:2] for day in _days(start, end)}
    expected_week_ends: dict[tuple[int, int], date] = {}
    expected_month_ends: dict[tuple[int, int], date] = {}
    for day in _days(start, end):
        expected_week_ends[day.isocalendar()[:2]] = day
        expected_month_ends[(day.year, day.month)] = day
    assert len(daily) == len(observations)
    assert len(weekly) == len(expected_weeks)
    assert len(monthly) == 120
    assert len(yearly) == 10
    assert weekly == [
        {"observed_at": day.isoformat(), "value": (day - start).days / 1000.0}
        for day in expected_week_ends.values()
    ]
    assert monthly == [
        {"observed_at": day.isoformat(), "value": (day - start).days / 1000.0}
        for day in expected_month_ends.values()
    ]
    assert [point["observed_at"] for point in yearly] == [
        f"{year}-12-31" for year in range(2016, 2026)
    ]
    assert yearly[-1]["value"] == (end - start).days / 1000.0
    series = macro_indicator_series(database, series_id="us.10y")
    assert series is not None
    assert series["tradingview_symbol"] == "TVC:US10Y"


def test_macro_indicator_series_filters_range_before_aggregation_and_keeps_latest_vintage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "macro.sqlite"
    connection = initialize_database(database)
    try:
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 1, day),
                    value=value,
                    unit="percent",
                    source_url="https://example.com/us10y.csv",
                    vintage_at=vintage,
                )
                for day, value, vintage in (
                    (1, 1.0, datetime(2026, 1, 2, tzinfo=UTC)),
                    (2, 2.0, datetime(2026, 1, 3, tzinfo=UTC)),
                    (2, 2.5, datetime(2026, 1, 4, tzinfo=UTC)),
                    (3, 3.0, datetime(2026, 1, 4, tzinfo=UTC)),
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()

    points = _points(
        database,
        "monthly",
        start=date(2026, 1, 1),
        end=date(2026, 1, 2),
    )

    assert points == [{"observed_at": "2026-01-02", "value": 2.5}]


def test_macro_indicator_series_applies_jquants_publication_cutoff(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    connection = initialize_database(database)
    try:
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id="jp.foreign_flows",
                    observed_at=date(2024, 8, 23),
                    value=value,
                    unit="jpy",
                    source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
                    period_start=date(2024, 8, 19),
                    period_end=date(2024, 8, 23),
                    vintage_at=vintage_at,
                )
                for value, vintage_at in (
                    (-408854431.0, datetime(2024, 8, 29, tzinfo=UTC)),
                    (-400000000.0, datetime(2024, 9, 10, tzinfo=UTC)),
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()

    august = macro_indicator_series(
        database,
        series_id="jp.foreign_flows",
        start=date(2024, 8, 1),
        end=date(2024, 8, 31),
        limit=None,
    )
    september = macro_indicator_series(
        database,
        series_id="jp.foreign_flows",
        start=date(2024, 8, 1),
        end=date(2024, 9, 30),
        limit=None,
    )

    assert august is not None
    assert august["points"] == [{"observed_at": "2024-08-23", "value": -408854431.0}]
    assert september is not None
    assert september["points"] == [{"observed_at": "2024-08-23", "value": -400000000.0}]


def test_macro_indicator_series_rejects_invalid_range_and_limit(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    initialize_database(database).close()

    with pytest.raises(ValueError, match="end must be on or after start"):
        macro_indicator_series(
            database,
            series_id="us.10y",
            start=date(2026, 2, 1),
            end=date(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="limit must be positive"):
        macro_indicator_series(database, series_id="us.10y", limit=0)


def test_macro_indicator_series_hides_retained_unregistered_metadata(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    connection = initialize_database(database)
    try:
        connection.execute(
            "INSERT INTO series("
            "series_id, name, category, geography, frequency, unit, provider, "
            "provider_series_id, source_id, source_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "retired.series",
                "Retired",
                "test",
                "world",
                "monthly",
                "index",
                "fred_csv",
                "RETIRED",
                "retired",
                "https://example.com/retired.csv",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert macro_indicator_series(database, series_id="retired.series") is None
    with sqlite3.connect(database) as check:
        assert (
            check.execute(
                "SELECT COUNT(*) FROM series WHERE series_id = 'retired.series'"
            ).fetchone()[0]
            == 1
        )


def _points(
    database: Path,
    granularity: MacroGranularity,
    *,
    start: date,
    end: date,
) -> list[dict[str, object]]:
    result = macro_indicator_series(
        database,
        series_id="us.10y",
        start=start,
        end=end,
        granularity=granularity,
        limit=None,
    )
    assert result is not None
    points = result["points"]
    assert isinstance(points, list)
    return points


def _days(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = start
    while current <= end:
        result.append(current)
        current += timedelta(days=1)
    return result
