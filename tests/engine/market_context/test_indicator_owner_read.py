from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import store_h15_observations

from baibai_engine.data_collection.economic.records import store_snapshot
from baibai_engine.data_collection.economic.store import initialize_database
from baibai_engine.machine_analysis.market_context.indicator_service import IndicatorsService
from baibai_engine.machine_analysis.market_context.indicators.definitions import load_definitions
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord
from baibai_engine.machine_analysis.market_context.reading import reader as reading_reader
from baibai_engine.machine_analysis.market_context.reading.query import reading_snapshot
from baibai_engine.machine_analysis.market_context.reading.rules import ReadingRulesError


def test_indicator_owner_filters_range_and_keeps_latest_vintage(
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

    values = (
        IndicatorsService(database)
        .get_range("us.10y", start=date(2026, 1, 1), end=date(2026, 1, 2))
        .observations
    )
    assert [(v.observed_at, v.value) for v in values] == [
        (date(2026, 1, 1), 1.0),
        (date(2026, 1, 2), 2.5),
    ]


def test_indicator_owner_derived_inputs_share_one_wal_snapshot(tmp_path: Path, monkeypatch) -> None:
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

    first = IndicatorsService(database).get_range("us.erp", start=day, end=day)
    assert updated
    assert [(v.observed_at, v.value) for v in first.observations] == [(day, 1.0)]

    second = IndicatorsService(database).get_range("us.erp", start=day, end=day)
    assert [(v.observed_at, v.value) for v in second.observations] == [(day, 2.0)]


def test_indicator_owner_applies_jquants_publication_cutoff(tmp_path: Path) -> None:
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

    august = IndicatorsService(database).get_range(
        "jp.foreign_flows", start=date(2024, 8, 1), end=date(2024, 8, 31)
    )
    september = IndicatorsService(database).get_range(
        "jp.foreign_flows", start=date(2024, 8, 1), end=date(2024, 9, 30)
    )
    assert [(v.observed_at, v.value) for v in august.observations] == [
        (date(2024, 8, 23), -408854431.0)
    ]
    assert [(v.observed_at, v.value) for v in september.observations] == [
        (date(2024, 8, 23), -400000000.0)
    ]


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

    from baibai_engine.investment_judgment.inputs import collect_research_inputs
    from baibai_engine.machine_analysis.market_context.reading.models import snapshot_payload
    from baibai_engine.machine_analysis.market_context.reading.query import read_store_snapshot
    from baibai_engine.machine_analysis.market_context.reading.rules import DEFAULT_RULES_PATH

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


def test_document_revision_replaces_observed_periods_without_old_fallback(tmp_path: Path) -> None:
    import json
    from dataclasses import replace

    from baibai_engine.data_collection.economic.documents import SourceDocument, store_document
    from baibai_engine.machine_analysis.market_context.indicators.definitions import (
        load_definitions,
    )

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
    before = IndicatorsService(database).get_range(
        "jp.bankruptcies", start=date.min, end=date(2026, 7, 12)
    )
    assert before.observations[-1].observed_at == date(2026, 6, 1)
    with closing(initialize_database(database)) as connection, connection:
        store_document(
            connection,
            replace(
                document,
                fetched_at=datetime(2026, 7, 11, tzinfo=UTC),
                payload=json.dumps([may], ensure_ascii=False).encode(),
            ),
        )
    after = IndicatorsService(database).get_range(
        "jp.bankruptcies", start=date.min, end=date(2026, 7, 12)
    )
    assert [(v.observed_at, v.value) for v in after.observations] == [(date(2026, 5, 1), 439.0)]
