from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.acquisition.economic_cli as cli
from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_rechecks
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

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


@pytest.mark.parametrize("kind", ["record", "document"])
@pytest.mark.parametrize("cloud_changed", [False, True])
def test_explicit_cli_recheck_survives_cloud_union_and_pending_older_stage(
    tmp_path, monkeypatch, kind, cloud_changed
):
    local, canonical = tmp_path / "local.sqlite", tmp_path / "canonical.sqlite"
    received(local, "A", STAMP, kind)
    received(canonical, "A", STAMP, kind)
    if cloud_changed:
        received(canonical, "B", STAMP + timedelta(hours=1), kind)
    actual = STAMP + timedelta(hours=2)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return actual.astimezone(tz)

    definition = SourceDefinition(
        "test",
        "fred_csv" if kind == "record" else "tsr_bankruptcies",
        "https://example.test/pmi.pdf",
        "daily",
    )
    monkeypatch.setattr(acquisition, "datetime", Clock)
    monkeypatch.setattr(acquisition, "load_sources", lambda: {"test": definition})
    monkeypatch.setattr(cli, "load_project_env", lambda: None)
    monkeypatch.setattr(
        acquisition, "fetch_native_records", lambda *args, **kwargs: {DAY: [{"value": "A"}]}
    )
    monkeypatch.setattr(
        acquisition,
        "fetch_source_documents",
        lambda *args, **kwargs: iter(
            [
                SourceDocument(
                    "test", "document", definition.source_url, "application/pdf", actual, b"A", {}
                )
            ]
        ),
    )
    assert (
        cli.main(
            [
                "refresh",
                "test",
                "--start",
                DAY.isoformat(),
                "--end",
                DAY.isoformat(),
                "--db",
                str(local),
            ]
        )
        == 0
    )
    # The existing push-indicators direction: fresh cloud history joins local acquisition.
    merge_stores(canonical, local)
    verify_stores(canonical, local)
    table = "economic_source_records" if kind == "record" else "economic_source_documents"
    with closing(initialize_database(local)) as conn:
        stamps = [
            row[0] for row in conn.execute(f"SELECT fetched_at FROM {table} ORDER BY fetched_at")
        ]
        assert stamps[-1] == actual.isoformat(timespec="microseconds")
        assert len(stamps) == (3 if cloud_changed else 2)
        if kind == "record":
            latest = next(read_snapshots(conn, DAY, DAY, source_id="test"))
            assert latest.records == ({"value": "A"},)
        else:
            assert next(read_documents(conn, "test")).payload == b"A"
    if not cloud_changed:
        # A true no-change operator receipt still prevents a delayed older B from winning.
        pending, accepted = tmp_path / "pending.sqlite", tmp_path / "accepted.sqlite"
        received(pending, "B", STAMP + timedelta(hours=1), kind)
        before = local.read_bytes()
        report = accept_rechecks(pending, local, accepted)
        assert len(report["late_recovery_required"]) == 1
        assert report["accepted_receipts"] == 0
        assert local.read_bytes() == before
