"""Complete confirmation proof streams exact anchors without per-period SQL."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

import baibai_engine.data_collection.receipts.native_sources.confirmations as confirmations
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    collection_digest,
    confirm_snapshot,
    confirm_source_response,
    save_collection_confirmation,
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


def test_native_confirmation_reads_its_complete_proof_once_per_clock(tmp_path, monkeypatch):
    scanned = []
    expected = []

    def count_scan(connection, source_id, checked_at):
        assert connection.in_transaction
        scanned.append((source_id, checked_at))
        return collection_digest(connection, source_id, checked_at)

    monkeypatch.setattr(confirmations, "collection_digest", count_scan)
    with closing(initialize_database(tmp_path / "source.sqlite")) as connection, connection:
        for hours, days in ((0, DAYS), (1, DAYS[1:])):
            fetched_at = CLOCK + timedelta(hours=hours)
            confirm_source_response(
                connection,
                source_id=SOURCE,
                fetched_at=fetched_at,
                records_by_day={day: [{"value": SOURCE}] for day in days},
            )
            checked = fetched_at.isoformat(timespec="microseconds")
            expected.append((SOURCE, checked))
            assert scanned == expected
            assert tuple(
                connection.execute(
                    "SELECT * FROM economic_source_collection_confirmations"
                ).fetchone()
            ) == (SOURCE, checked, collection_digest(connection, SOURCE, checked))


def test_native_confirmation_rechecks_proof_if_no_write_transaction(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite"
    initialize_database(path).close()
    scans = []

    with (
        closing(sqlite3.connect(path, autocommit=True)) as connection,
        closing(sqlite3.connect(path)) as competitor,
    ):

        def scan(target, source_id, checked_at):
            assert not target.in_transaction
            proof = collection_digest(target, source_id, checked_at)
            scans.append(proof)
            if len(scans) == 1:
                # The second owner can update this snapshot only because no write
                # transaction protects the acquisition's first proof.
                with competitor:
                    confirm_snapshot(
                        competitor,
                        source_id=SOURCE,
                        observed_at=DAYS[1],
                        fetched_at=CLOCK,
                        records=[{"value": "competing period"}],
                    )
            return proof

        monkeypatch.setattr(confirmations, "collection_digest", scan)
        with pytest.raises(ValueError, match="differs from its period set"):
            confirm_source_response(
                connection,
                source_id=SOURCE,
                fetched_at=CLOCK,
                records_by_day={DAYS[0]: [{"value": SOURCE}]},
            )
        assert len(set(scans)) == 2
        assert (
            connection.execute("SELECT * FROM economic_source_collection_confirmations").fetchone()
            is None
        )


def test_native_confirmation_rejects_a_conflicting_prior_proof_at_the_same_clock(tmp_path):
    with closing(initialize_database(tmp_path / "source.sqlite")) as connection, connection:
        records = {DAYS[0]: [{"value": SOURCE}]}
        confirm_source_response(
            connection, source_id=SOURCE, fetched_at=CLOCK, records_by_day=records
        )
        connection.execute(
            "UPDATE economic_source_collection_confirmations SET content_sha256=? WHERE source_id=?",
            ("0" * 64, SOURCE),
        )
        with pytest.raises(ValueError, match="same received time"):
            confirm_source_response(
                connection, source_id=SOURCE, fetched_at=CLOCK, records_by_day=records
            )


def test_saved_confirmation_checks_receiving_anchors_without_changing_clock_conflicts(tmp_path):
    with closing(initialize_database(tmp_path / "source.sqlite")) as connection, connection:
        day = DAYS[0]
        records = [{"value": SOURCE}]
        confirm_source_response(
            connection, source_id=SOURCE, fetched_at=CLOCK, records_by_day={day: records}
        )
        row = tuple(
            connection.execute("SELECT * FROM economic_source_collection_confirmations").fetchone()
        )
        forged = "0" * 64
        stale = (CLOCK - timedelta(hours=1)).isoformat(timespec="microseconds")
        save_collection_confirmation(connection, (SOURCE, stale, forged))
        with pytest.raises(ValueError, match="same received time"):
            save_collection_confirmation(connection, (SOURCE, row[1], forged))
        save_collection_confirmation(connection, row)

        fetched_at = CLOCK + timedelta(hours=1)
        checked = fetched_at.isoformat(timespec="microseconds")
        confirm_snapshot(
            connection,
            source_id=SOURCE,
            observed_at=day,
            fetched_at=fetched_at,
            records=records,
        )
        with pytest.raises(ValueError, match="differs from its period set"):
            save_collection_confirmation(connection, (SOURCE, checked, forged))
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM economic_source_collection_confirmations"
                ).fetchone()
            )
            == row
        )
        save_collection_confirmation(connection, (SOURCE, checked, row[2]))
        assert tuple(
            connection.execute("SELECT * FROM economic_source_collection_confirmations").fetchone()
        ) == (SOURCE, checked, row[2])
        connection.execute(
            "UPDATE economic_source_records SET payload=? WHERE source_id=?",
            (b"invalid receiving anchor", SOURCE),
        )
        with pytest.raises(ValueError, match="period anchor"):
            save_collection_confirmation(connection, (SOURCE, checked, row[2]))


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
