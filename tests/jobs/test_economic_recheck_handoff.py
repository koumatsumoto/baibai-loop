from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.economic.documents import SourceDocument, store_document
from baibai_engine.data_collection.economic.handoff import export_receipts
from baibai_engine.data_collection.economic.receipt_policy import retain_rechecks
from baibai_engine.data_collection.economic.records import store_snapshot
from baibai_engine.data_collection.economic.store import initialize_database
from baibai_jobs.storage.economic_receipts import accept_rechecks
from baibai_jobs.storage.merge_economic_store import merge_stores, verify_stores

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def received(path, value, at, kind):
    with closing(initialize_database(path)) as conn, conn:
        if kind == "record":
            return store_snapshot(
                conn, source_id="test", observed_at=DAY, fetched_at=at, records=[{"value": value}]
            )
        return store_document(
            conn,
            SourceDocument(
                "test",
                "document",
                "https://example.test/pmi.pdf",
                "application/pdf",
                at,
                value.encode(),
                {},
            ),
        )


@pytest.mark.parametrize(
    ("kind", "table"),
    [("record", "economic_source_records"), ("document", "economic_source_documents")],
)
def test_stale_seed_equal_response_keeps_real_receipt_then_restores_canonical_a(
    tmp_path, kind, table
):
    seed, canonical, raw, accepted = (
        tmp_path / name
        for name in ("seed.sqlite", "canonical.sqlite", "raw.sqlite", "accepted.sqlite")
    )
    received(seed, "A", STAMP, kind)
    received(canonical, "A", STAMP, kind)
    received(canonical, "B", STAMP + timedelta(hours=1), kind)
    actual = STAMP + timedelta(hours=2)
    with retain_rechecks():
        assert received(seed, "A", actual, kind)
    # This is the real response time, not the seed's old receipt given a new time.
    export_receipts(seed, raw, source_ids=["test"], since=actual)
    report = accept_rechecks(raw, canonical, accepted)
    assert report["accepted_receipts"] == 1
    assert report["late_recovery_required"] == []
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    with closing(initialize_database(canonical)) as conn:
        assert [
            tuple(row)
            for row in conn.execute(f"SELECT fetched_at FROM {table} ORDER BY fetched_at")
        ] == [
            (STAMP.isoformat(timespec="microseconds"),),
            ((STAMP + timedelta(hours=1)).isoformat(timespec="microseconds"),),
            (actual.isoformat(timespec="microseconds"),),
        ]
    # Rechecking the same actual A produces no extra canonical vintage.
    second = tmp_path / "second.sqlite"
    report = accept_rechecks(raw, canonical, second)
    merge_stores(second, canonical)
    verify_stores(second, canonical)
    with closing(initialize_database(canonical)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 3


def test_equal_current_value_is_not_another_canonical_vintage(tmp_path):
    seed, canonical, raw, accepted = (
        tmp_path / name
        for name in ("seed.sqlite", "canonical.sqlite", "raw.sqlite", "accepted.sqlite")
    )
    received(seed, "A", STAMP, "record")
    received(canonical, "A", STAMP, "record")
    actual = STAMP + timedelta(hours=1)
    with retain_rechecks():
        received(seed, "A", actual, "record")
    export_receipts(seed, raw, source_ids=["test"], since=actual)
    report = accept_rechecks(raw, canonical, accepted)
    assert report["unchanged_rechecks"] == 1
    assert report["accepted_receipts"] == 0
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    with closing(initialize_database(canonical)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1


def test_unintegrated_old_receipt_requires_explicit_recovery(tmp_path):
    raw, canonical, accepted = (
        tmp_path / name for name in ("raw.sqlite", "canonical.sqlite", "accepted.sqlite")
    )
    received(raw, "A", STAMP, "record")
    received(canonical, "B", STAMP + timedelta(hours=1), "record")
    before = canonical.read_bytes()
    report = accept_rechecks(raw, canonical, accepted)
    assert len(report["late_recovery_required"]) == 1
    assert report["accepted_receipts"] == 0
    assert canonical.read_bytes() == before
