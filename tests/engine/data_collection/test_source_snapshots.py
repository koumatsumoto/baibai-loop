"""Daily native source payloads share storage without sharing identities."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from tests.helpers.network import _no_real_network as _no_real_network

from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
    stored_days,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database


def test_source_identity_isolates_same_date_vintages_and_empty_responses(tmp_path):
    day = date(2026, 10, 14)
    first = datetime(2026, 10, 15, tzinfo=UTC)
    corrected = first + timedelta(hours=1)
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn, conn:
        for source, rows in (("one", [{"long": "3", "short": "1"}]), ("two", [{"other": None}])):
            store_snapshot(conn, source_id=source, observed_at=day, fetched_at=first, records=rows)
        store_snapshot(conn, source_id="one", observed_at=day, fetched_at=corrected, records=[])
        assert next(read_snapshots(conn, day, day, source_id="one")).records == ()
        assert next(read_snapshots(conn, day, day, source_id="two")).records == ({"other": None},)
        assert stored_days(conn, day, day, source_id="one") == {day}
        assert stored_days(conn, day, day, source_id="absent") == set()
