"""Complete confirmation proof streams exact anchors without per-period SQL."""

import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    collection_digest,
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database

SOURCE = "fred-test"
CLOCK = datetime(2026, 11, 6, tzinfo=UTC)
DAYS = (date(1900, 1, 1), date(2025, 1, 1), date(2026, 1, 1))


def seed(connection):
    for source in (SOURCE, "other-source"):
        confirm_source_response(
            connection,
            source_id=source,
            fetched_at=CLOCK,
            records_by_day={day: [{"value": source}] for day in DAYS},
        )
    confirm_source_response(
        connection,
        source_id=SOURCE,
        fetched_at=CLOCK + timedelta(hours=1),
        records_by_day={day: [{"value": SOURCE}] for day in DAYS[1:]},
    )
    store_snapshot(
        connection,
        source_id=SOURCE,
        observed_at=date(1900, 1, 2),
        fetched_at=CLOCK,
        records=[],
    )


def test_same_digest_with_confirmed_empty_and_legacy_empty_uses_two_selects(tmp_path):
    with closing(initialize_database(tmp_path / "source.sqlite")) as connection, connection:
        seed(connection)
        checked, expected = connection.execute(
            "SELECT checked_at,content_sha256 FROM economic_source_collection_confirmations "
            "WHERE source_id=?",
            (SOURCE,),
        ).fetchone()
        periods = connection.execute(
            "SELECT observed_at,content_sha256 FROM economic_source_record_confirmations "
            "WHERE source_id=? AND observed_at IN (?,?) ORDER BY observed_at",
            (SOURCE, *(str(day) for day in DAYS[1:])),
        ).fetchall()
        exact = hashlib.sha256(
            json.dumps([tuple(row) for row in periods], separators=(",", ":")).encode()
        ).hexdigest()
        statements = []
        connection.set_trace_callback(statements.append)
        assert collection_digest(connection, SOURCE, checked) == exact == expected
        assert len(statements) == 2
        assert all(sql.startswith("SELECT ") for sql in statements)


@pytest.mark.parametrize("damage", ["missing", "payload", "clock", "future", "unconfirmed"])
def test_outside_window_and_zero_anchors_are_still_validated(tmp_path, damage):
    with closing(initialize_database(tmp_path / "source.sqlite")) as connection, connection:
        seed(connection)
        checked = (CLOCK + timedelta(hours=1)).isoformat(timespec="microseconds")
        # The vanished 1900 period is outside any current Reading window.
        key = (SOURCE, str(DAYS[0]))
        if damage == "missing":
            connection.execute(
                "DELETE FROM economic_source_records WHERE source_id=? AND observed_at=?", key
            )
        elif damage == "payload":
            connection.execute(
                "UPDATE economic_source_records SET payload=? "
                "WHERE source_id=? AND observed_at=? AND fetched_at=?",
                (b"corrupt empty", *key, checked),
            )
        elif damage == "clock":
            connection.execute(
                "UPDATE economic_source_record_confirmations SET checked_at=? "
                "WHERE source_id=? AND observed_at=?",
                (CLOCK.isoformat(timespec="microseconds"), *key),
            )
        elif damage == "future":
            connection.execute(
                "UPDATE economic_source_record_confirmations SET adopted_fetched_at=? "
                "WHERE source_id=? AND observed_at=?",
                ((CLOCK + timedelta(hours=2)).isoformat(timespec="microseconds"), *key),
            )
        else:
            connection.execute(
                "DELETE FROM economic_source_record_confirmations WHERE source_id=? AND observed_at=?",
                (SOURCE, str(DAYS[1])),
            )
        with pytest.raises(ValueError, match=r"period anchor|missing a known native period"):
            collection_digest(connection, SOURCE, checked)
