import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.receipts.native_sources import store as store
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.cli.market_reading import main as reading_main


def test_read_only_connection_encodes_uri_metacharacters_and_rejects_writes(tmp_path):
    real_db = tmp_path / "real.sqlite"
    store.initialize_database(real_db).close()
    crafted = tmp_path / "real.sqlite?mode=rw&x="
    crafted.touch()
    with (
        closing(store.open_read_only_connection(crafted)) as connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("CREATE TABLE write_probe(value INTEGER)")
    with closing(sqlite3.connect(real_db)) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='write_probe'").fetchone()
            is None
        )


def test_missing_canonical_source_store_requires_explicit_provisioning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="explicit provisioning"):
        store.open_connection()
    assert not store.DEFAULT_DB_PATH.exists()


def test_reading_cli_preserves_source_publication_cutoff(tmp_path, capsys):
    database = tmp_path / "sources.sqlite"
    with closing(store.initialize_database(database)) as connection, connection:
        for published, value in [
            (date(2024, 8, 29), -408854431.0),
            (date(2024, 9, 10), -400000000.0),
        ]:
            store_snapshot(
                connection,
                source_id="jquants-trades-spec",
                observed_at=published,
                fetched_at=datetime.combine(published, datetime.min.time(), UTC),
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
    assert reading_main(["--asof", "2024-08-31", "--db", str(database), "--format", "json"]) == 0
    rows = {item["series_id"]: item for item in json.loads(capsys.readouterr().out)["series"]}
    assert rows["jp.foreign_flows"]["latest_value"] == -408854431.0
    assert rows["jp.foreign_flows"]["next_print_estimate"] == "2024-09-06"
    assert rows["jp.foreign_flows"]["print_due_in_days"] == 6


@pytest.mark.parametrize("mutation", ["missing-column", "missing-key"])
def test_source_schema_requires_columns_and_primary_keys(tmp_path, mutation):
    path = tmp_path / "sources.sqlite"
    with closing(store.initialize_database(path)) as connection:
        connection.execute("DROP TABLE economic_source_documents")
        if mutation == "missing-column":
            connection.execute("CREATE TABLE economic_source_documents(source_id TEXT)")
        else:
            connection.execute(
                "CREATE TABLE economic_source_documents(source_id TEXT, request_key TEXT, "
                "fetched_at TEXT, source_url TEXT, content_type TEXT, payload BLOB, metadata TEXT)"
            )
        with pytest.raises(store.EconomicSchemaError, match="table contract"):
            store.validate_current_schema(connection)


def test_native_withdrawal_removes_derived_reading_without_scalar_history(tmp_path):
    from tests.helpers.indicator_store import (
        observation,
        store_h15_observations,
        store_mof_observations,
    )

    from baibai_engine.machine_analysis.observations.economic_indicators.service import IndicatorsService
    from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
        load_definitions,
    )
    from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
        build_store_observation_reader,
    )

    path = tmp_path / "sources.sqlite"
    definitions = load_definitions()
    registry = definitions.by_id()
    day = date(2026, 7, 20)
    vintage = datetime(2026, 7, 21, tzinfo=UTC)
    with closing(store.initialize_database(path)) as connection, connection:
        store_mof_observations(connection, [observation(registry["jp.10y"], day, 1.6, vintage)])
        store_h15_observations(connection, [observation(registry["us.10y"], day, 4.5, vintage)])
    reader = IndicatorsService(path)
    before = reader.get_range("rate_diff.us_jp_10y", start=day, end=day)
    assert len(before.observations) == 1
    assert before.observations[0].value == pytest.approx(2.9)
    with closing(store.open_connection(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id=registry["jp.10y"].source_id,
            observed_at=day,
            fetched_at=datetime(2026, 7, 22, tzinfo=UTC),
            records=[],
        )
    assert reader.get_range("rate_diff.us_jp_10y", start=day, end=day).observations == ()
    with closing(store.open_read_only_connection(path)) as connection:
        read = build_store_observation_reader(connection, series=definitions.series)
        assert read("jp.10y", day, day) == ()


def test_native_receipt_identity_replay_and_conflict_leave_newer_revision_intact(tmp_path):
    from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots

    day = date(2026, 10, 15)
    old_jst = datetime.fromisoformat("2026-10-16T09:00:00+09:00")
    new_utc = datetime(2026, 10, 16, 1, tzinfo=UTC)
    old, new = [{"raw": 1}], [{"raw": 2}]
    with closing(store.initialize_database(tmp_path / "sources.sqlite")) as connection, connection:
        assert store_snapshot(
            connection, source_id="test-source", observed_at=day, fetched_at=old_jst, records=old
        )
        assert store_snapshot(
            connection, source_id="test-source", observed_at=day, fetched_at=new_utc, records=new
        )
        before = connection.execute(
            "SELECT fetched_at, record_count, payload FROM economic_source_records ORDER BY fetched_at"
        ).fetchall()
        assert not store_snapshot(
            connection,
            source_id="test-source",
            observed_at=day,
            fetched_at=old_jst.astimezone(UTC),
            records=old,
        )
        with pytest.raises(ValueError, match="conflicting source receipt identity"):
            store_snapshot(
                connection,
                source_id="test-source",
                observed_at=day,
                fetched_at=old_jst,
                records=[{"raw": 3}],
            )
        assert (
            connection.execute(
                "SELECT fetched_at, record_count, payload FROM economic_source_records ORDER BY fetched_at"
            ).fetchall()
            == before
        )
        assert next(read_snapshots(connection, day, day, source_id="test-source")).records == (
            new[0],
        )
