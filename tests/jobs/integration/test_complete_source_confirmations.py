from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.integration.native_source_merge as publication
from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.sqlite.merge import MergeError

SOURCE = "fred-test"
DAY = date(2026, 11, 1)
NEW = DAY + timedelta(days=1)
ARTIFICIAL = DAY + timedelta(days=2)
STAMP = datetime(2026, 11, 5, tzinfo=UTC)


def receive(path, values, hour, source_id=SOURCE):
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id=source_id,
            fetched_at=STAMP + timedelta(hours=hour),
            records_by_day={day: [{"value": value}] for day, value in values.items()},
        )


def apply(raw, canonical, output):
    report = accept_rechecks(raw, canonical, output)
    publication.merge_stores(output, canonical)
    publication.verify_stores(output, canonical)
    return report


def current(path):
    with closing(initialize_database(path)) as connection:
        validate_collection_confirmations(connection)
        return {
            row.observed_at: (row.records, row.fetched_at)
            for row in read_snapshots(connection, DAY, ARTIFICIAL, source_id=SOURCE)
        }


@pytest.mark.parametrize("order", [(2, 3), (3, 2)])
def test_full_response_removes_unknown_period_and_blocks_older_resurrection(tmp_path, order):
    canonical = tmp_path / "canonical"
    receive(canonical, {DAY: "A"}, 1)
    with closing(initialize_database(canonical)) as connection, connection:
        store_snapshot(
            connection,
            source_id=SOURCE,
            observed_at=ARTIFICIAL,
            fetched_at=STAMP,
            records=[],
        )
    packets = {}
    for hour, values in [(2, {DAY: "A", NEW: "B"}), (3, {DAY: "A"})]:
        local, raw = tmp_path / f"local{hour}", tmp_path / f"raw{hour}"
        receive(local, {DAY: "A"}, 1)
        receive(local, values, hour)
        export_receipts(local, raw, source_ids=[SOURCE], since=STAMP + timedelta(hours=hour))
        packets[hour] = raw
    for hour in order:
        report = apply(packets[hour], canonical, tmp_path / f"accepted{hour}")
        assert bool(report["late_recovery_required"]) == (hour == 2 and order[0] == 3)
    result = current(canonical)
    assert result[DAY][0] == ({"value": "A"},)
    if order[0] == 2:
        assert result[NEW] == ((), STAMP + timedelta(hours=3))
    else:
        assert NEW not in result
    with closing(initialize_database(canonical)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM economic_source_record_confirmations WHERE observed_at=?",
                (str(ARTIFICIAL),),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM economic_source_records WHERE observed_at=?",
                (str(ARTIFICIAL),),
            ).fetchone()[0]
            == 1
        )


def test_same_clock_with_a_different_complete_period_set_is_a_conflict(tmp_path):
    canonical, raw = tmp_path / "canonical", tmp_path / "raw"
    receive(canonical, {DAY: "A"}, 3)
    receive(raw, {DAY: "A", NEW: "B"}, 3)
    before = canonical.read_bytes()
    with pytest.raises(EconomicReceiptConflict, match="same received time"):
        accept_rechecks(raw, canonical, tmp_path / "accepted")
    assert canonical.read_bytes() == before


def test_no_change_export_is_self_contained_and_has_no_duplicate_body(tmp_path):
    source, exported = tmp_path / "source", tmp_path / "exported"
    receive(source, {DAY: "A", NEW: "."}, 1)
    receive(source, {DAY: "A", NEW: "."}, 3)
    export_receipts(source, exported, source_ids=[SOURCE], since=STAMP + timedelta(hours=3))
    assert current(source) == current(exported)
    for path in (source, exported):
        with closing(initialize_database(path)) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0]
                == 2
            )
            assert connection.execute(
                "SELECT checked_at FROM economic_source_collection_confirmations"
            ).fetchone()[0] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")


def test_incomplete_export_cannot_claim_a_complete_source_response(tmp_path):
    canonical, raw = tmp_path / "canonical", tmp_path / "raw"
    receive(canonical, {DAY: "A"}, 1)
    receive(raw, {DAY: "A", NEW: "B"}, 2)
    with closing(initialize_database(raw)) as connection, connection:
        connection.execute("DELETE FROM economic_source_records WHERE observed_at=?", (str(NEW),))
    with pytest.raises(EconomicReceiptConflict, match="period anchor"):
        accept_rechecks(raw, canonical, tmp_path / "accepted")


def test_legacy_artificial_empty_does_not_block_first_real_native_period(tmp_path):
    canonical, local = tmp_path / "canonical", tmp_path / "local"
    for path in (canonical, local):
        with closing(initialize_database(path)) as connection, connection:
            store_snapshot(
                connection, source_id=SOURCE, observed_at=NEW, fetched_at=STAMP, records=[]
            )
        receive(path, {DAY: "A"}, 1)
    receive(local, {DAY: "A", NEW: "B"}, 2)
    apply(local, canonical, tmp_path / "accepted")
    assert current(canonical)[NEW] == (({"value": "B"},), STAMP + timedelta(hours=2))
    with closing(initialize_database(canonical)) as connection:
        assert (
            connection.execute(
                "SELECT record_count FROM economic_source_records WHERE observed_at=? AND fetched_at=?",
                (str(NEW), STAMP.isoformat(timespec="microseconds")),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("lost_local_state", [False, True])
def test_explicit_recovery_rechecks_fresh_canonical_and_preserves_originals(
    tmp_path, monkeypatch, failed, lost_local_state
):
    canonical, local = tmp_path / "canonical", tmp_path / "local"
    receive(canonical, {DAY: "A"}, 1)
    with closing(initialize_database(canonical)) as connection, connection:
        connection.execute("DELETE FROM economic_source_record_confirmations")
        connection.execute("DELETE FROM economic_source_collection_confirmations")
    receive(local, {DAY: "B", NEW: "late local"}, 3)
    # A different unchanged local source must not prevent single-source recovery.
    for path in (canonical, local):
        receive(path, {DAY: "old"}, 1, source_id="fred-other")
    receive(canonical, {DAY: "new"}, 2, source_id="fred-other")
    with pytest.raises(MergeError, match="explicit source recheck"):
        publication.reconcile_stores(canonical, local)
    if lost_local_state:
        with closing(initialize_database(local)) as connection, connection:
            connection.execute(
                "DELETE FROM economic_source_record_confirmations WHERE source_id=?", (SOURCE,)
            )
    before = [path.read_bytes() for path in (canonical, local)]
    source = SourceDefinition(SOURCE, "fred_csv", "https://example.test/complete", "daily", "TEST")
    monkeypatch.setattr(publication, "load_sources", lambda: {SOURCE: source})
    monkeypatch.setattr(acquisition, "load_sources", lambda: {SOURCE: source})

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (STAMP + timedelta(hours=4)).astimezone(tz)

    def fetch(*args, **kwargs):
        if failed:
            raise ValueError("synthetic failed response")
        return {DAY: [{"value": "C"}]}

    monkeypatch.setattr(acquisition, "datetime", Clock)
    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    if failed:
        with pytest.raises(MergeError, match="recovery failed"):
            publication.reconcile_stores(canonical, local, recheck_sources=(SOURCE,))
        assert [path.read_bytes() for path in (canonical, local)] == before
    else:
        publication.reconcile_stores(canonical, local, recheck_sources=(SOURCE,))
        assert canonical.read_bytes() == before[0]
        result = current(local)
        assert result[DAY] == (({"value": "C"},), STAMP + timedelta(hours=4))
        assert result[NEW] == ((), STAMP + timedelta(hours=4))
        with closing(initialize_database(local)) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM economic_source_records WHERE source_id=?", (SOURCE,)
                ).fetchone()[0]
                == 5
            )


def test_period_confirmation_without_complete_response_cannot_bypass_source_order(tmp_path):
    canonical, local = tmp_path / "canonical", tmp_path / "local"
    receive(canonical, {DAY: "A"}, 3)
    receive(local, {NEW: "B"}, 2)
    with closing(initialize_database(local)) as connection, connection:
        connection.execute("DELETE FROM economic_source_collection_confirmations")
    before = canonical.read_bytes()
    report = apply(local, canonical, tmp_path / "accepted")
    assert report["late_recovery_required"] == [[SOURCE, "*", "collection_confirmation_missing"]]
    assert canonical.read_bytes() == before
    assert NEW not in current(canonical)


def test_an_unchanged_stale_other_source_is_a_noop_not_a_late_acquisition(tmp_path):
    canonical, local = tmp_path / "canonical", tmp_path / "local"
    for path in (canonical, local):
        receive(path, {DAY: "A"}, 1)
        receive(path, {DAY: "old"}, 1, source_id="fred-other")
    receive(canonical, {DAY: "new"}, 2, source_id="fred-other")
    receive(local, {DAY: "B"}, 3)
    publication.reconcile_stores(canonical, local)
    assert current(local)[DAY] == (({"value": "B"},), STAMP + timedelta(hours=3))
    with closing(initialize_database(local)) as connection:
        other = next(read_snapshots(connection, DAY, DAY, source_id="fred-other"))
        assert other.records == ({"value": "new"},)
        assert other.fetched_at == STAMP + timedelta(hours=2)
    publication.reconcile_stores(canonical, local)


def test_recovery_does_not_adopt_an_old_local_change_clock_as_the_new_baseline(
    tmp_path, monkeypatch
):
    canonical, local = tmp_path / "canonical", tmp_path / "local"
    receive(canonical, {DAY: "A"}, 1)
    receive(canonical, {DAY: "A"}, 5)
    with closing(initialize_database(canonical)) as connection, connection:
        connection.execute("DELETE FROM economic_source_record_confirmations")
        connection.execute("DELETE FROM economic_source_collection_confirmations")
    receive(local, {DAY: "B"}, 4)
    source = SourceDefinition(SOURCE, "fred_csv", "https://example.test/complete", "daily", "TEST")
    monkeypatch.setattr(publication, "load_sources", lambda: {SOURCE: source})
    monkeypatch.setattr(acquisition, "load_sources", lambda: {SOURCE: source})
    monkeypatch.setattr(
        acquisition, "fetch_native_records", lambda *args, **kwargs: {DAY: [{"value": "B"}]}
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (STAMP + timedelta(hours=6)).astimezone(tz)

    monkeypatch.setattr(acquisition, "datetime", Clock)
    publication.reconcile_stores(canonical, local, recheck_sources=(SOURCE,))
    assert current(local)[DAY] == (({"value": "B"},), STAMP + timedelta(hours=6))
    with closing(initialize_database(local)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 3
        state = connection.execute(
            "SELECT checked_at,adopted_fetched_at FROM economic_source_record_confirmations"
        ).fetchone()
        assert (
            tuple(state) == ((STAMP + timedelta(hours=6)).isoformat(timespec="microseconds"),) * 2
        )


def test_schema12_same_value_recheck_keeps_one_body_set_and_one_actual_fetch(tmp_path, monkeypatch):
    from baibai_jobs.maintenance.migrate_economic_store import migrate

    old, canonical, local = (tmp_path / name for name in ("old", "canonical", "local"))
    days = [DAY - timedelta(days=offset) for offset in range(100)]
    with closing(initialize_database(old)) as connection, connection:
        for day in days:
            store_snapshot(
                connection,
                source_id=SOURCE,
                observed_at=day,
                fetched_at=STAMP,
                records=[{"value": "A"}],
            )
        connection.execute("DROP TABLE economic_source_record_confirmations")
        connection.execute("DROP TABLE economic_source_collection_confirmations")
        connection.execute("PRAGMA user_version=12")
    old_bytes = old.read_bytes()
    migrate(old, canonical)
    with closing(initialize_database(local)):
        pass
    source = SourceDefinition(SOURCE, "fred_csv", "https://example.test/complete", "daily", "TEST")
    monkeypatch.setattr(publication, "load_sources", lambda: {SOURCE: source})
    monkeypatch.setattr(acquisition, "load_sources", lambda: {SOURCE: source})
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (STAMP + timedelta(hours=6)).astimezone(tz)

    def fetch(*args, **kwargs):
        calls.append("GET")
        return {day: [{"value": "A"}] for day in days}

    monkeypatch.setattr(acquisition, "datetime", Clock)
    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    publication.reconcile_stores(canonical, local, recheck_sources=(SOURCE,))
    assert calls == ["GET"]
    assert old.read_bytes() == old_bytes
    with closing(initialize_database(local)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 100
        )
        assert [
            tuple(row)
            for row in connection.execute("SELECT record_count FROM economic_source_runs")
        ] == [(100,)]
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT checked_at,adopted_fetched_at FROM economic_source_record_confirmations"
            )
        } == {
            (
                (STAMP + timedelta(hours=6)).isoformat(timespec="microseconds"),
                STAMP.isoformat(timespec="microseconds"),
            )
        }
