from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.indicator_store import store_h15_observations

from baibai_engine.market_context.indicators.db import (
    get_series,
    initialize_database,
    insert_observations,
    retract_observations,
)
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import store_snapshot
from baibai_engine.market_context.reading.rules import ReadingRulesError
from baibai_engine.read_api.freshness import indicator_latest_observed_at
from baibai_engine.read_api.market_context import (
    IndicatorGranularity,
    indicator_series,
    reading_snapshot,
)


def test_indicator_series_aggregates_each_period_to_its_last_observation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "indicators.sqlite"
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
        store_h15_observations(connection, observations)
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
    series = indicator_series(database, series_id="us.10y")
    assert series is not None
    assert series["tradingview_symbol"] == "TVC:US10Y"


def test_indicator_series_filters_range_before_aggregation_and_keeps_latest_vintage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "indicators.sqlite"
    connection = initialize_database(database)
    try:
        store_h15_observations(
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


def test_indicator_series_applies_jquants_publication_cutoff(tmp_path: Path) -> None:
    database = tmp_path / "indicators.sqlite"
    connection = initialize_database(database)
    try:
        for published, value in [
            (date(2024, 8, 29), -408854431.0),
            (date(2024, 9, 10), -400000000.0),
        ]:
            store_snapshot(
                connection,
                source_key="jquants_flows:TSEPrime",
                observed_at=published,
                fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
                records=[
                    {
                        "Section": "TSEPrime",
                        "PubDate": published.isoformat(),
                        "StDate": "2024-08-19",
                        "EnDate": "2024-08-23",
                        "FrgnBal": value,
                    }
                ],
            )
        connection.commit()
    finally:
        connection.close()

    august = indicator_series(
        database,
        series_id="jp.foreign_flows",
        start=date(2024, 8, 1),
        end=date(2024, 8, 31),
        limit=None,
    )
    september = indicator_series(
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


def test_reading_snapshot_clamps_only_provider_declared_point_in_time_vintages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "indicators.sqlite"
    connection = initialize_database(database)
    try:
        for published, value in [(date(2024, 8, 29), 100.0), (date(2024, 9, 10), 120.0)]:
            store_snapshot(
                connection,
                source_key="jquants_flows:TSEPrime",
                observed_at=published,
                fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
                records=[
                    {
                        "Section": "TSEPrime",
                        "PubDate": published.isoformat(),
                        "StDate": "2024-08-19",
                        "EnDate": "2024-08-23",
                        "FrgnBal": value,
                    }
                ],
            )
        definition = get_series(connection, "jp.nikkei225")
        for value, vintage_at in (
            (100.0, datetime(2024, 8, 29, tzinfo=UTC)),
            (120.0, datetime(2024, 9, 10, tzinfo=UTC)),
        ):
            store_snapshot(
                connection,
                source_key=native_source_key(definition),
                observed_at=date(2024, 8, 23),
                fetched_at=vintage_at,
                records=[
                    {"observation_date": "2024-08-23", definition.provider_series_id: str(value)}
                ],
            )
        connection.commit()
    finally:
        connection.close()

    payload = reading_snapshot(database, asof=date(2024, 8, 31))

    assert payload is not None
    readings = {str(item["series_id"]): item for item in payload["series"]}  # type: ignore[index]
    assert readings["jp.foreign_flows"]["latest_value"] == 100.0
    # FRED bulk-history vintage is acquisition time, not publication time, so it
    # remains visible before the store happened to acquire it.
    assert readings["jp.nikkei225"]["latest_value"] == 120.0

    semantics = payload["read_semantics"]
    assert semantics["point_in_time_guaranteed"] is False
    assert semantics["known_at_applied"] is False
    assert (
        readings["jp.foreign_flows"]["read_semantics"]["vintage_selection"]
        == "publication_vintage_cutoff"
    )
    assert readings["jp.foreign_flows"]["read_semantics"]["vintage_cutoff"] == "2024-08-31"
    assert (
        readings["jp.nikkei225"]["read_semantics"]["vintage_selection"] == "latest_stored_history"
    )
    assert readings["jp.nikkei225"]["read_semantics"]["vintage_cutoff"] is None

    from tests.helpers.research_selection import selection

    from baibai_engine.market_context.reading.models import snapshot_payload
    from baibai_engine.market_context.reading.query import read_store_snapshot
    from baibai_engine.market_context.reading.rules import DEFAULT_RULES_PATH
    from baibai_engine.research.inputs import collect_research_inputs
    from baibai_web.readmodel.models import ReadingView

    selected = selection(now=datetime(2024, 8, 31, tzinfo=UTC))
    inputs = collect_research_inputs(
        snapshot=selected.inputs.analysis,
        input_gaps=(),
        tickers=(),
        research_as_of=date(2024, 8, 31),
        input_known_at=datetime(2024, 8, 31, tzinfo=UTC),
        market_path=tmp_path / "missing-market.sqlite",
        indicators_path=database,
        app_path=tmp_path / "missing-app.sqlite",
    )
    assert inputs.market_context["reading"] == payload
    web = ReadingView.model_validate({**payload, "as_of": payload["asof"], "fetch_health": []})
    assert web.read_semantics.limitation == semantics["limitation"]
    assert web.series[0].read_semantics.limitation == semantics["limitation"]
    with closing(sqlite3.connect(database)) as con:
        con.row_factory = sqlite3.Row
        pure = snapshot_payload(
            read_store_snapshot(con, asof=date(2024, 8, 31), rules_path=DEFAULT_RULES_PATH)
        )
    assert "read_semantics" not in pure
    for pure_row in pure["series"]:
        assert "read_semantics" not in pure_row
        enriched = {
            k: v for k, v in readings[pure_row["series_id"]].items() if k != "read_semantics"
        }
        assert enriched == pure_row


def test_reading_snapshot_fails_closed_when_rules_are_missing(tmp_path: Path) -> None:
    database = tmp_path / "indicators.sqlite"
    initialize_database(database).close()

    with pytest.raises(ReadingRulesError, match="failed to read Reading rules"):
        reading_snapshot(
            database,
            asof=date(2026, 7, 26),
            rules_path=tmp_path / "missing-rules.yaml",
        )


def test_indicator_series_rejects_invalid_range_and_limit(tmp_path: Path) -> None:
    database = tmp_path / "indicators.sqlite"
    initialize_database(database).close()

    with pytest.raises(ValueError, match="end must be on or after start"):
        indicator_series(
            database,
            series_id="us.10y",
            start=date(2026, 2, 1),
            end=date(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="limit must be positive"):
        indicator_series(database, series_id="us.10y", limit=0)


def test_indicator_series_hides_retained_unregistered_metadata(tmp_path: Path) -> None:
    database = tmp_path / "indicators.sqlite"
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

    assert indicator_series(database, series_id="retired.series") is None
    with closing(sqlite3.connect(database)) as check:
        assert (
            check.execute(
                "SELECT COUNT(*) FROM series WHERE series_id = 'retired.series'"
            ).fetchone()[0]
            == 1
        )


def test_retracted_observations_leave_the_chart_and_the_freshness_date(tmp_path: Path) -> None:
    """A withdrawn observation is not a point on the chart, nor the date the store is fresh to."""

    database = tmp_path / "indicators.sqlite"
    connection = initialize_database(database)
    try:
        series = get_series(connection, "jp.bankruptcies")
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id="jp.bankruptcies",
                    observed_at=observed_at,
                    value=value,
                    unit=series.unit,
                    source_url=series.source_url,
                    vintage_at=datetime.combine(observed_at, datetime.min.time(), tzinfo=UTC),
                )
                for observed_at, value in (
                    (date(2026, 5, 1), 439.0),
                    (date(2026, 6, 1), 441.0),
                )
            ],
        )
        retract_observations(
            connection,
            "jp.bankruptcies",
            [
                (
                    date(2026, 6, 1),
                    datetime.combine(date(2026, 6, 1), datetime.min.time(), tzinfo=UTC),
                )
            ],
            vintage_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
        connection.commit()
    finally:
        connection.close()

    chart = indicator_series(database, series_id="jp.bankruptcies", granularity="daily")

    assert chart is not None
    assert chart["points"] == [{"observed_at": "2026-05-01", "value": 439.0}]
    assert indicator_latest_observed_at(database) == date(2026, 5, 1)


def _points(
    database: Path,
    granularity: IndicatorGranularity,
    *,
    start: date,
    end: date,
) -> list[dict[str, object]]:
    result = indicator_series(
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
