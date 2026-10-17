from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.collection.economic_records import read_snapshots, store_snapshot
from baibai_engine.market_context import indicator_service as service
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_fetch_cache_latest_and_rebuild_share_publication_vintage_selection(
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
    with closing(db.initialize_database(path)) as connection:
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

    monkeypatch.setattr(service, "fetch_native_records", fetch)
    monkeypatch.setattr(service, "_today_jst", lambda: asof)
    reader = service.IndicatorsService(path)
    fresh = reader.get_range(definition.series_id, start=observed_at, end=asof, refresh=True)
    cached = reader.get_range(definition.series_id, start=observed_at, end=asof)
    latest = reader.get_latest(definition.series_id, refresh=True)
    rebuilt = reader.refresh_all_history(definition.series_id, end=asof)
    assert fresh.observations == cached.observations == latest.observations == rebuilt.observations
    assert [item.value for item in fresh.observations] == [200]
    assert fresh.observations[0].vintage_at == datetime(2024, 8, 30, tzinfo=UTC)
    assert not fresh.cache_hit
    assert cached.cache_hit
    assert not latest.cache_hit
    assert not rebuilt.cache_hit
    assert len(calls) == 3
    with closing(db.open_read_only_connection(path)) as connection:
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
        assert [row[0] for row in connection.execute("SELECT record_count FROM provider_runs")] == [
            2,
            2,
            2,
        ]


@pytest.mark.parametrize("failure_point", ["store", "run"])
def test_native_refresh_failure_rolls_back_the_source_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    path = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 1)
    replies = iter(
        [
            {day: [{"row": {"RIFLGFCY10_N.B": "4.0"}}]},
            {day: [{"row": {"RIFLGFCY10_N.B": "4.1"}}]},
        ]
    )
    monkeypatch.setattr(service, "fetch_native_records", lambda *a, **k: next(replies))
    reader = service.IndicatorsService(path)
    original = reader.get_range("us.10y", start=day, end=day, refresh=True)
    if failure_point == "store":
        write = service.store_snapshot

        def fail_after_write(connection, **kwargs):
            write(connection, **kwargs)
            raise RuntimeError("source update interrupted")

        monkeypatch.setattr(service, "store_snapshot", fail_after_write)
    else:
        record = db.record_provider_run

        def fail_success_record(connection, **kwargs):
            if kwargs["status"] == "ok":
                raise RuntimeError("source update interrupted")
            return record(connection, **kwargs)

        monkeypatch.setattr(db, "record_provider_run", fail_success_record)
    with pytest.raises(RuntimeError, match="source update interrupted"):
        reader.get_range("us.10y", start=day, end=day, refresh=True)
    assert reader.get_range("us.10y", start=day, end=day).observations == original.observations
    with closing(db.open_read_only_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
        failed = connection.execute(
            "SELECT record_count FROM provider_runs WHERE status = 'failed'"
        ).fetchall()
        assert [row[0] for row in failed] == [0]


def test_vintage_order_and_identity_use_instants_not_timezone_spelling(tmp_path: Path) -> None:
    definition = load_definitions().by_id()["us.10y"]
    day = date(2026, 10, 15)
    old_jst = datetime.fromisoformat("2026-10-16T09:00:00+09:00")
    new_utc = datetime.fromisoformat("2026-10-16T01:00:00+00:00")
    with closing(db.initialize_database(tmp_path / "indicators.sqlite")) as connection:
        db.insert_observations(connection, [observation(definition, day, 1.0, old_jst)])
        db.insert_observations(connection, [observation(definition, day, 2.0, new_utc)])
        selected = db.latest_observation(connection, definition.series_id)
        assert selected is not None
        assert selected.value == 2.0
        assert selected.vintage_at == new_utc
        assert db.observations_in_range(connection, definition.series_id, day, day) == (selected,)
        db.insert_observations(
            connection,
            [observation(definition, day, 1.0, old_jst.astimezone(UTC))],
            deduplicate_unchanged=False,
        )
        assert db.row_count(connection, "observations") == 2
        assert db.latest_observation(connection, definition.series_id) == selected


def test_observation_vintage_without_timezone_is_not_guessed() -> None:
    definition = load_definitions().by_id()["us.10y"]
    with pytest.raises(ValueError, match="vintage_at must include a timezone"):
        observation(
            definition,
            date(2026, 10, 15),
            1.0,
            datetime(2026, 10, 16, tzinfo=UTC).replace(tzinfo=None),
        )
