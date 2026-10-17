from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.indicator_store import store_h15_observations

from baibai_engine.collection.economic_records import store_snapshot
from baibai_engine.collection.economic_store import initialize_database
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading import reader as reading_reader
from baibai_engine.market_context.reading.rules import ReadingRulesError
from baibai_engine.read_api.freshness import indicator_latest_observed_at
from baibai_engine.read_api.market_context import (
    IndicatorGranularity,
    indicator_fetch_health,
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


def test_indicator_series_derived_inputs_share_one_wal_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 1)
    registry = load_definitions().by_id()
    first_receipt = datetime(2026, 7, 2, tzinfo=UTC)
    second_receipt = datetime(2026, 7, 3, tzinfo=UTC)

    def save_sources(
        connection: sqlite3.Connection, earnings: str, yield_10y: str, receipt: datetime
    ) -> None:
        store_snapshot(
            connection,
            source_id=registry["us.sp500_earnings_yield"].source_id,
            observed_at=day,
            fetched_at=receipt,
            records=[{"date_text": "Jul 1, 2026", "value_text": earnings, "table_row": 1}],
        )
        store_snapshot(
            connection,
            source_id=registry["us.10y"].source_id,
            observed_at=day,
            fetched_at=receipt,
            records=[
                {
                    "row": {"Time Period": day.isoformat(), "RIFLGFCY10_N.B": yield_10y},
                    "preamble": [],
                }
            ],
        )

    with closing(initialize_database(database)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        save_sources(writer, "5", "4", first_receipt)
        writer.commit()

    native_read = reading_reader.read_native_series
    updated = False

    def read_then_update(connection, series, *, start, end, vintage_cutoff=None):
        nonlocal updated
        observations = native_read(
            connection, series, start=start, end=end, vintage_cutoff=vintage_cutoff
        )
        if series.series_id == "us.sp500_earnings_yield" and not updated:
            assert [item.value for item in observations] == [5.0]
            with closing(sqlite3.connect(database)) as writer:
                save_sources(writer, "7", "5", second_receipt)
                writer.commit()
            updated = True
        return observations

    monkeypatch.setattr(reading_reader, "read_native_series", read_then_update)

    first = indicator_series(database, series_id="us.erp", start=day, end=day)
    assert updated
    assert first is not None
    assert first["points"] == [{"observed_at": day.isoformat(), "value": 1.0}]

    second = indicator_series(database, series_id="us.erp", start=day, end=day)
    assert second is not None
    assert second["points"] == [{"observed_at": day.isoformat(), "value": 2.0}]


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
                source_id="jquants-trades-spec",
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
                source_id="jquants-trades-spec",
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
        definition = load_definitions().by_id()["jp.nikkei225"]
        for value, vintage_at in (
            (100.0, datetime(2024, 8, 29, tzinfo=UTC)),
            (120.0, datetime(2024, 9, 10, tzinfo=UTC)),
        ):
            store_snapshot(
                connection,
                source_id=definition.source_id,
                observed_at=date(2024, 8, 23),
                fetched_at=vintage_at,
                records=[{"observation_date": "2024-08-23", definition.reading_key: str(value)}],
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


def test_indicator_series_does_not_treat_an_unreferenced_source_as_a_series(
    tmp_path: Path,
) -> None:
    database = tmp_path / "indicators.sqlite"
    with closing(initialize_database(database)) as connection, connection:
        store_snapshot(
            connection,
            source_id="unreferenced-source",
            observed_at=date(2026, 5, 1),
            fetched_at=datetime(2026, 5, 2, tzinfo=UTC),
            records=[{"raw": 1}],
        )

    assert indicator_series(database, series_id="retired.series") is None
    with closing(sqlite3.connect(database)) as check:
        assert (
            check.execute(
                "SELECT COUNT(*) FROM economic_source_records "
                "WHERE source_id = 'unreferenced-source'"
            ).fetchone()[0]
            == 1
        )


def test_source_health_is_projected_to_its_external_series_only(tmp_path: Path) -> None:
    database = tmp_path / "sources.sqlite"
    with closing(initialize_database(database)) as connection, connection:
        for run_id, source_id, finished_at, status in [
            ("a", "fed-h15", "2026-07-20T00:00:00.000000+00:00", "failed"),
            ("b", "fed-h15", "2026-07-20T00:00:01.000000+00:00", "ok"),
            ("c", "unreferenced-source", "2026-07-20T00:00:02.000000+00:00", "failed"),
        ]:
            connection.execute(
                "INSERT INTO economic_source_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    source_id,
                    "2026-07-20",
                    "2026-07-20",
                    finished_at,
                    finished_at,
                    status,
                    42,
                    None if status == "ok" else "unavailable",
                ),
            )
    before = database.read_bytes()
    result = indicator_fetch_health(database)
    expected = {
        item.series_id
        for item in load_definitions().series
        if item.source_id == "fed-h15" and item.provider != "derived"
    }
    assert expected
    assert {item["series_id"] for item in result} == expected
    assert all(item["status"] == "ok" and item["record_count"] == 42 for item in result)
    assert all(item["error_message"] is None for item in result)
    assert database.read_bytes() == before


def test_document_revision_changes_chart_and_freshness(tmp_path: Path) -> None:
    import json
    from dataclasses import replace

    from baibai_engine.collection.economic_documents import SourceDocument, store_document
    from baibai_engine.market_context.indicators.definitions import load_definitions

    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.bankruptcies"]
    may = {"period_division": "月次", "title": "2026年5月 全国企業倒産 439件"}
    june = {"period_division": "月次", "title": "2026年6月 全国企業倒産 441件"}
    document = SourceDocument(
        source_id=series.source_id,
        request_key="history",
        source_url=series.source_url,
        content_type="application/json",
        fetched_at=datetime(2026, 7, 10, tzinfo=UTC),
        payload=json.dumps([may, june], ensure_ascii=False).encode(),
        metadata={"params": {"data": "data"}},
    )
    with closing(initialize_database(database)) as connection, connection:
        store_document(connection, document)
    assert indicator_latest_observed_at(database) == date(2026, 6, 1)
    with closing(initialize_database(database)) as connection, connection:
        store_document(
            connection,
            replace(
                document,
                fetched_at=datetime(2026, 7, 11, tzinfo=UTC),
                payload=json.dumps([may], ensure_ascii=False).encode(),
            ),
        )
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
