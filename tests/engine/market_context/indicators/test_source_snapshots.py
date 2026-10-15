"""Daily native source payloads share storage without sharing identities."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from baibai_engine.market_context.indicators.db import initialize_database
from baibai_engine.market_context.indicators.source_snapshots import (
    read_snapshots,
    store_snapshot,
    stored_days,
)


def test_source_identity_isolates_same_date_vintages_and_empty_responses(tmp_path):
    day = date(2026, 10, 14)
    first = datetime(2026, 10, 15, tzinfo=UTC)
    corrected = first + timedelta(hours=1)
    with closing(initialize_database(tmp_path / "indicators.sqlite")) as conn, conn:
        for source, rows in (("one", [{"long": "3", "short": "1"}]), ("two", [{"other": None}])):
            store_snapshot(conn, source_key=source, observed_at=day, fetched_at=first, records=rows)
        store_snapshot(conn, source_key="one", observed_at=day, fetched_at=corrected, records=[])
        assert list(read_snapshots(conn, day, day, source_key="one"))[0].records == ()
        assert list(read_snapshots(conn, day, day, source_key="two"))[0].records == ({"other": None},)
        assert stored_days(conn, day, day, source_key="one") == {day}
        assert stored_days(conn, day, day, source_key="absent") == set()
