from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.collection import economic_acquisition as acquisition
from baibai_engine.collection import economic_store
from baibai_engine.collection.economic_records import read_snapshots, store_snapshot
from baibai_engine.market_context import indicator_service as service
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_explicit_refresh_and_reads_share_publication_vintage_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["jp.foreign_flows"]
    observed_at = date(2024, 8, 23)
    asof = date(2024, 8, 31)
    publications = {
        published: [
            {
                "Section": "TSEPrime",
                "PubDate": published.isoformat(),
                "StDate": "2024-08-19",
                "EnDate": observed_at.isoformat(),
                "FrgnBal": value,
            }
        ]
        for published, value in [(date(2024, 8, 29), 100), (date(2024, 8, 30), 200)]
    }
    future_date = date(2024, 9, 10)
    with closing(economic_store.initialize_database(path)) as connection:
        store_snapshot(
            connection,
            source_id="jquants-trades-spec",
            observed_at=future_date,
            fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
            records=[
                {
                    "Section": "TSEPrime",
                    "PubDate": future_date.isoformat(),
                    "StDate": "2024-08-19",
                    "EnDate": observed_at.isoformat(),
                    "FrgnBal": 300,
                }
            ],
        )
        connection.commit()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return publications

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    monkeypatch.setattr(service, "_today_jst", lambda: asof)
    reader = service.IndicatorsService(path)
    source_ids = [definition.source_id]
    first = acquisition.refresh_sources(source_ids, db_path=path, start=observed_at, end=asof)
    assert all(isinstance(item, acquisition.SourceRefreshSuccess) for item in first)
    fresh = reader.get_range(definition.series_id, start=observed_at, end=asof)
    repeated = reader.get_range(definition.series_id, start=observed_at, end=asof)
    latest = reader.get_latest(definition.series_id)
    assert len(calls) == 1
    second = acquisition.refresh_sources(source_ids, db_path=path, start=observed_at, end=asof)
    assert all(isinstance(item, acquisition.SourceRefreshSuccess) for item in second)
    refreshed = reader.get_range(definition.series_id, start=observed_at, end=asof)
    assert (
        fresh.observations == repeated.observations == latest.observations == refreshed.observations
    )
    assert [item.value for item in fresh.observations] == [200]
    assert fresh.observations[0].vintage_at == datetime(2024, 8, 30, tzinfo=UTC)
    assert len(calls) == 2
    with closing(economic_store.open_read_only_connection(path)) as connection:
        future = build_store_observation_reader(
            connection, series=[definition], vintage_cutoff=future_date
        )
        assert [item.value for item in future(definition.series_id, observed_at, observed_at)] == [
            300
        ]
        assert (
            next(
                read_snapshots(
                    connection, future_date, future_date, source_id="jquants-trades-spec"
                )
            ).records[0]["FrgnBal"]
            == 300
        )
        # Two received publication records produce one selected L2 observation.
        assert [
            row[0]
            for row in connection.execute(
                "SELECT record_count FROM economic_source_runs ORDER BY started_at, rowid"
            )
        ] == [2, 2]


def _h15_response(day: date, value: str) -> dict[date, list[dict[str, object]]]:
    return {
        day: [
            {
                "row": {"Time Period": day.isoformat(), "RIFLGFCY10_N.B": value},
                "preamble": [],
            }
        ]
    }


def test_native_store_snapshot_failure_rolls_back_source_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 1)
    source_id = load_definitions().by_id()["us.10y"].source_id
    replies = iter([_h15_response(day, "4.0"), _h15_response(day, "4.1")])
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: next(replies))
    reader = service.IndicatorsService(path)
    first = acquisition.refresh_sources([source_id], db_path=path, start=day, end=day)
    assert isinstance(first[0], acquisition.SourceRefreshSuccess)
    write = acquisition.store_snapshot

    def fail_after_write(connection, **kwargs):
        write(connection, **kwargs)
        raise RuntimeError("source update interrupted")

    monkeypatch.setattr(acquisition, "store_snapshot", fail_after_write)
    failure = acquisition.refresh_sources([source_id], db_path=path, start=day, end=day)
    assert isinstance(failure[0], acquisition.SourceRefreshFailure)
    assert "source update interrupted" in failure[0].message
    assert reader.get_range("us.10y", start=day, end=day).observations[0].value == 4.0
    with closing(economic_store.open_read_only_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
        assert [
            row[0]
            for row in connection.execute(
                "SELECT record_count FROM economic_source_runs WHERE status = 'failed'"
            )
        ] == [0]


def test_success_run_record_failure_keeps_committed_raw_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 1)
    source_id = load_definitions().by_id()["us.10y"].source_id
    replies = iter([_h15_response(day, "4.0"), _h15_response(day, "4.1")])
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: next(replies))
    reader = service.IndicatorsService(path)
    first = acquisition.refresh_sources([source_id], db_path=path, start=day, end=day)
    assert isinstance(first[0], acquisition.SourceRefreshSuccess)
    record = economic_store.record_source_run

    def fail_success_record(connection, **kwargs):
        if kwargs["status"] == "ok":
            raise RuntimeError("source update interrupted")
        return record(connection, **kwargs)

    monkeypatch.setattr(economic_store, "record_source_run", fail_success_record)
    failure = acquisition.refresh_sources([source_id], db_path=path, start=day, end=day)
    assert isinstance(failure[0], acquisition.SourceRefreshFailure)
    assert "source update interrupted" in failure[0].message
    assert reader.get_range("us.10y", start=day, end=day).observations[0].value == 4.1
    with closing(economic_store.open_read_only_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 2


def test_vintage_order_and_identity_use_instants_not_timezone_spelling(tmp_path: Path) -> None:
    definition = load_definitions().by_id()["us.10y"]
    day = date(2026, 10, 15)
    old_jst = datetime.fromisoformat("2026-10-16T09:00:00+09:00")
    new_utc = datetime.fromisoformat("2026-10-16T01:00:00+00:00")
    database = tmp_path / "indicators.sqlite"

    def h15_row(value: str) -> dict[str, object]:
        return {"row": {"Time Period": day.isoformat(), "RIFLGFCY10_N.B": value}, "preamble": []}

    with closing(economic_store.initialize_database(database)) as connection, connection:
        store_snapshot(
            connection,
            source_id=definition.source_id,
            observed_at=day,
            fetched_at=old_jst,
            records=[h15_row("1.0")],
        )
        store_snapshot(
            connection,
            source_id=definition.source_id,
            observed_at=day,
            fetched_at=new_utc,
            records=[h15_row("2.0")],
        )
        assert next(
            read_snapshots(connection, day, day, source_id=definition.source_id)
        ).records == (h15_row("2.0"),)
        reader = build_store_observation_reader(connection, series=[definition])
        assert reader(definition.series_id, day, day)[0].value == 2.0
        assert [
            row[0]
            for row in connection.execute(
                "SELECT fetched_at FROM economic_source_records ORDER BY fetched_at"
            )
        ] == [
            old_jst.astimezone(UTC).isoformat(timespec="microseconds"),
            new_utc.isoformat(timespec="microseconds"),
        ]
        assert not store_snapshot(
            connection,
            source_id=definition.source_id,
            observed_at=day,
            fetched_at=old_jst.astimezone(UTC),
            records=[h15_row("1.0")],
        )
        assert economic_store.row_count(connection, "economic_source_records") == 2
        assert reader(definition.series_id, day, day)[0].value == 2.0


def test_observation_vintage_without_timezone_is_not_guessed() -> None:
    definition = load_definitions().by_id()["us.10y"]
    with pytest.raises(ValueError, match="vintage_at must include a timezone"):
        observation(
            definition,
            date(2026, 10, 15),
            1.0,
            datetime(2026, 10, 16, tzinfo=UTC).replace(tzinfo=None),
        )
