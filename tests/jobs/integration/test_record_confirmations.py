from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.receipts.native_sources.confirmations import confirm_snapshot
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

DAY = date(2025, 1, 1)
STAMP = datetime(2026, 11, 1, tzinfo=UTC)


def record(path, value, hour):
    with closing(initialize_database(path)) as conn, conn:
        confirm_snapshot(
            conn,
            source_id="fred-test",
            observed_at=DAY,
            fetched_at=STAMP + timedelta(hours=hour),
            records=[{"value": value}],
        )


def apply(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def read(path):
    with closing(initialize_database(path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0]
        state = tuple(conn.execute("SELECT * FROM economic_source_record_confirmations").fetchone())
        value = next(read_snapshots(conn, DAY, DAY, source_id="fred-test")).records[0]["value"]
        return count, state, value


def test_same_value_updates_confirmation_without_a_new_body_and_blocks_late_revision(tmp_path):
    canonical, raw, late = (tmp_path / name for name in ("current", "same", "late"))
    record(canonical, "A", 1)
    record(raw, "A", 3)
    result = apply(raw, canonical, tmp_path / "accepted")
    count, state, value = read(canonical)
    assert result["unchanged_rechecks"] == 1
    assert count == 1
    assert state[2] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")
    assert state[3] == (STAMP + timedelta(hours=1)).isoformat(timespec="microseconds")
    record(late, "B", 2)
    result = apply(late, canonical, tmp_path / "late-accepted")
    assert result["late_recovery_required"]
    assert read(canonical) == (count, state, value)


def test_a_b_a_keeps_last_change_even_when_a_matches_stale_seed(tmp_path):
    canonical, seed, raw = (tmp_path / name for name in ("current", "seed", "raw"))
    record(canonical, "A", 1)
    record(seed, "A", 1)
    record(canonical, "B", 2)
    record(seed, "A", 3)
    export_receipts(seed, raw, source_ids=["fred-test"], since=STAMP + timedelta(hours=3))
    result = apply(raw, canonical, tmp_path / "accepted")
    assert not result["late_recovery_required"]
    count, state, value = read(canonical)
    assert (count, value) == (3, "A")
    assert state[2] == state[3] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")
    apply(raw, canonical, tmp_path / "retry")
    assert read(canonical) == (count, state, value)


def test_no_change_export_carries_exact_anchor_and_actual_confirmation(tmp_path):
    source, exported = tmp_path / "source", tmp_path / "exported"
    record(source, "A", 1)
    record(source, "A", 3)
    assert read(source)[0] == 1
    export_receipts(source, exported, source_ids=["fred-test"], since=STAMP + timedelta(hours=3))
    assert read(exported) == read(source)


def test_direct_union_does_not_override_a_later_equal_confirmation(tmp_path):
    current, old = tmp_path / "current", tmp_path / "old"
    record(current, "A", 1)
    record(current, "A", 3)
    record(old, "B", 2)
    merge_stores(old, current)
    assert read(current)[2] == "A"


def test_missing_confirmation_holds_received_content_for_explicit_recheck(tmp_path):
    canonical, raw = tmp_path / "current", tmp_path / "raw"
    record(canonical, "A", 1)
    record(canonical, "A", 3)
    with closing(initialize_database(canonical)) as conn, conn:
        conn.execute("DELETE FROM economic_source_record_confirmations")
    record(raw, "B", 2)
    before = canonical.read_bytes()
    result = accept_rechecks(raw, canonical, tmp_path / "accepted")
    assert result["late_recovery_required"] == [["fred-test", str(DAY), "confirmation_missing"]]
    assert canonical.read_bytes() == before
    # Actual operator recheck repairs the copy; no old change time is relabelled.
    record(canonical, "A", 4)
    assert read(canonical)[1][2] == (STAMP + timedelta(hours=4)).isoformat(timespec="microseconds")


def test_same_confirmation_clock_with_different_content_is_rejected(tmp_path):
    canonical, raw = tmp_path / "current", tmp_path / "raw"
    record(canonical, "A", 1)
    record(canonical, "A", 3)
    record(raw, "B", 3)
    before = canonical.read_bytes()
    with pytest.raises(EconomicReceiptConflict, match="same received time"):
        accept_rechecks(raw, canonical, tmp_path / "accepted")
    assert canonical.read_bytes() == before


def test_confirmation_anchor_is_verified_before_acceptance(tmp_path):
    canonical, raw = tmp_path / "current", tmp_path / "raw"
    record(canonical, "A", 1)
    record(raw, "B", 2)
    with closing(initialize_database(raw)) as conn, conn:
        conn.execute("UPDATE economic_source_record_confirmations SET content_sha256='bad'")
    with pytest.raises(EconomicReceiptConflict, match="differs from its source receipt"):
        accept_rechecks(raw, canonical, tmp_path / "accepted")


def test_scoped_acquisition_context_does_not_duplicate_fred_content(tmp_path):
    from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_rechecks

    source = tmp_path / "source"
    with retain_rechecks():
        record(source, "A", 1)
        record(source, "A", 3)
    count, state, value = read(source)
    assert (count, value) == (1, "A")
    assert state[2] != state[3]


def test_late_unconfirmed_body_does_not_make_next_equal_recheck_a_change(tmp_path):
    current, old = tmp_path / "current", tmp_path / "old"
    record(current, "A", 1)
    record(current, "A", 3)
    record(old, "B", 2)
    merge_stores(old, current)
    record(current, "A", 4)
    count, state, value = read(current)
    assert (count, value) == (2, "A")
    assert state[3] == (STAMP + timedelta(hours=1)).isoformat(timespec="microseconds")


def test_direct_union_refuses_a_stale_content_anchor_instead_of_losing_a_b_a(tmp_path):
    from baibai_engine.storage.sqlite.merge import MergeError

    canonical, stale = tmp_path / "current", tmp_path / "stale"
    record(canonical, "A", 1)
    record(canonical, "B", 2)
    record(stale, "A", 1)
    record(stale, "A", 3)
    before = canonical.read_bytes()
    with pytest.raises(MergeError, match="fresh-canonical handoff acceptance"):
        merge_stores(stale, canonical)
    assert canonical.read_bytes() == before
    apply(stale, canonical, tmp_path / "accepted")
    assert read(canonical)[0::2] == (3, "A")


@pytest.mark.parametrize("seeded", [False, True])
def test_change_then_equal_recheck_keeps_original_change_receipt(tmp_path, seeded):
    canonical, raw = tmp_path / "current", tmp_path / "raw"
    with closing(initialize_database(canonical)):
        pass
    if seeded:
        record(canonical, "A", 1)
    record(raw, "A", 1)
    record(raw, "B", 2)
    record(raw, "B", 3)
    result = apply(raw, canonical, tmp_path / "accepted")
    assert not result["late_recovery_required"]
    count, state, value = read(canonical)
    assert (count, value) == (2, "B")
    assert state[3] == (STAMP + timedelta(hours=2)).isoformat(timespec="microseconds")
    assert state[2] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")


@pytest.mark.parametrize("hour", [2, 4])
def test_unconfirmed_handoff_cannot_bypass_existing_confirmation(tmp_path, hour):
    canonical, raw = tmp_path / "current", tmp_path / "raw"
    record(canonical, "A", 1)
    record(canonical, "A", 3)
    record(raw, "B", hour)
    with closing(initialize_database(raw)) as conn, conn:
        conn.execute("DELETE FROM economic_source_record_confirmations")
    before = read(canonical)
    result = apply(raw, canonical, tmp_path / "accepted")
    assert result["late_recovery_required"]
    assert read(canonical) == before


def test_fresh_disappearance_does_not_turn_a_legacy_empty_into_source_evidence(tmp_path):
    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot

    path = tmp_path / "source"
    with closing(initialize_database(path)) as conn, conn:
        for hour, payload in [(1, [{"value": "A"}]), (2, [])]:
            store_snapshot(
                conn,
                source_id="fred-test",
                observed_at=DAY,
                fetched_at=STAMP + timedelta(hours=hour),
                records=payload,
            )
        assert confirm_snapshot(
            conn,
            source_id="fred-test",
            observed_at=DAY,
            fetched_at=STAMP + timedelta(hours=3),
            records=[],
        )
        state = tuple(conn.execute("SELECT * FROM economic_source_record_confirmations").fetchone())
        assert (
            state[2] == state[3] == (STAMP + timedelta(hours=3)).isoformat(timespec="microseconds")
        )
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 3
