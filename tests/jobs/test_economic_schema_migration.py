from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import (
    EconomicSchemaError,
    initialize_database,
    open_connection,
)
from baibai_jobs.maintenance.migrate_economic_store import migrate


def test_offline_migration_preserves_receipts_without_inventing_confirmations(tmp_path):
    source, output = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    with closing(initialize_database(source)) as conn, conn:
        store_snapshot(
            conn,
            source_id="fred-test",
            observed_at=date(2025, 1, 1),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            records=[],
        )
        conn.execute("DROP TABLE economic_source_record_confirmations")
        conn.execute("PRAGMA user_version=12")
        originals = list(conn.execute("SELECT * FROM economic_source_records"))
    before = source.read_bytes()
    with pytest.raises(EconomicSchemaError):
        open_connection(source)
    migrate(source, output)
    assert source.read_bytes() == before
    with closing(initialize_database(output)) as conn:
        assert list(conn.execute("SELECT * FROM economic_source_records")) == originals
        assert (
            conn.execute("SELECT COUNT(*) FROM economic_source_record_confirmations").fetchone()[0]
            == 0
        )
    with pytest.raises(FileExistsError):
        migrate(source, output)
