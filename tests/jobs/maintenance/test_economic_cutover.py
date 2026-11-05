import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import R2Error
from baibai_jobs.maintenance import economic_cutover as cutover
from baibai_jobs.publication.lease import LeaseError, _load_handle, _save_handle, acquire, release

DAY = date(2020, 1, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def prepared(tmp_path):
    base = tmp_path / "base.sqlite"
    with closing(initialize_database(base)) as conn, conn:
        store_snapshot(
            conn, source_id="fred-payems", observed_at=DAY, fetched_at=AT, records=[{"value": "A"}]
        )
        store_snapshot(
            conn,
            source_id="fred-payems",
            observed_at=DAY + timedelta(days=1),
            fetched_at=AT,
            records=[],
        )
        conn.execute("DROP TABLE economic_source_record_confirmations")
        conn.execute("DROP TABLE economic_source_collection_confirmations")
        conn.execute("PRAGMA user_version=12")
    store = MemoryR2Store()
    for key in cutover.BUNDLE_KEYS:
        store.store_object(key, base.read_bytes() if key == cutover.KEY else key.encode())
        (tmp_path / f"{key}.etag").write_text(store.head(key).etag)
    store.store_object(
        cutover.MANIFEST,
        json.dumps(
            {
                "schema_version": 1,
                "keys": {key: store.head(key).etag for key in cutover.BUNDLE_KEYS},
            }
        ).encode(),
    )
    handle, _ = acquire(store, "operator")
    handle_path = tmp_path / "lease.json"
    _save_handle(handle_path, handle)
    output = tmp_path / "converted.sqlite"
    receipt = tmp_path / "cutover.json"
    cutover.prepare_copy(base, output, receipt, tmp_path)
    with closing(initialize_database(output)) as conn, conn:
        confirm_source_response(
            conn,
            source_id="fred-payems",
            fetched_at=AT + timedelta(hours=1),
            records_by_day={DAY: [{"value": "A"}]},
        )
    return store, base, output, receipt, handle_path


def test_publish_and_retry_read_back_exact_bytes_without_l2_or_reupload(prepared):
    store, base, output, receipt, handle = prepared
    before = base.read_bytes()
    assert cutover.freeze(receipt) == {"unconfirmed_native_periods": 0}
    store.put_keys.clear()
    result = cutover.publish(store, receipt, handle)
    assert result["readback_verified"]
    assert store.put_keys == [cutover.KEY, cutover.MANIFEST]
    value = json.loads(receipt.read_bytes())
    assert store.get_bytes(cutover.KEY) == Path(value["candidate"]["path"]).read_bytes()
    assert (
        json.loads(store.get_bytes(cutover.MANIFEST))["keys"][cutover.KEY]
        == store.head(cutover.KEY).etag
    )
    assert base.read_bytes() == before
    assert (receipt.parent / f"{cutover.KEY}.etag").read_text() == value["base_generations"][
        cutover.KEY
    ]
    assert not (receipt.parent / "lake").exists()
    store.put_keys.clear()
    assert cutover.publish(store, receipt, handle) == result
    assert store.put_keys == []
    # Retrying uses the frozen upload, never new edits to the working copy.
    with closing(initialize_database(output)) as conn, conn:
        conn.execute("DELETE FROM economic_source_records")
    assert cutover.publish(store, receipt, handle) == result


@pytest.mark.parametrize("key", [cutover.KEY, cutover.MANIFEST])
def test_lost_put_acknowledgement_is_resolved_by_actual_readback(prepared, monkeypatch, key):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.put_file

    def timeout(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        if target == key:
            raise R2Error("acknowledgement lost")
        return result

    monkeypatch.setattr(store, "put_file", timeout)
    assert cutover.publish(store, receipt, handle)["readback_verified"]
    assert store.put_keys.count(key) == 1


@pytest.mark.parametrize("key", [cutover.KEY, cutover.MANIFEST])
def test_unknown_outcome_keeps_same_candidate_and_resumes_without_resending(
    prepared, monkeypatch, key
):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    put, download = store.put_file, store.download_file
    landed = False

    def upload(target, *args, **kwargs):
        nonlocal landed
        result = put(target, *args, **kwargs)
        if target == key:
            landed = True
        return result

    def unavailable(target, *args, **kwargs):
        if target == key and landed:
            raise R2Error("readback unavailable")
        return download(target, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", upload)
    monkeypatch.setattr(store, "download_file", unavailable)
    with pytest.raises(R2Error, match="readback unavailable"):
        cutover.publish(store, receipt, handle)
    assert "completed" not in json.loads(receipt.read_bytes())
    monkeypatch.setattr(store, "download_file", download)
    assert cutover.publish(store, receipt, handle)["readback_verified"]
    assert store.put_keys.count(key) == 1


def test_known_unwritten_put_retries_original_candidate(prepared, monkeypatch):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.put_file

    def unavailable(*args, **kwargs):
        raise R2Error("write did not arrive")

    monkeypatch.setattr(store, "put_file", unavailable)
    with pytest.raises(ValueError, match="PUT not confirmed"):
        cutover.publish(store, receipt, handle)
    assert "completed" not in json.loads(receipt.read_bytes())
    monkeypatch.setattr(store, "put_file", original)
    assert cutover.publish(store, receipt, handle)["readback_verified"]


@pytest.mark.parametrize("key", [*cutover.BUNDLE_KEYS, cutover.MANIFEST])
def test_changed_remote_is_not_overwritten(prepared, key):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    store.store_object(key, b"someone else's generation")
    store.put_keys.clear()
    with pytest.raises((ValueError, json.JSONDecodeError)):
        cutover.publish(store, receipt, handle)
    assert store.put_keys == []


def test_peer_change_after_indicators_stops_before_manifest(prepared, monkeypatch):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.put_file

    def concurrent(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        if target == cutover.KEY:
            store.store_object("runs.sqlite", b"changed")
        return result

    monkeypatch.setattr(store, "put_file", concurrent)
    store.put_keys.clear()
    with pytest.raises(ValueError, match="peer changed"):
        cutover.publish(store, receipt, handle)
    assert store.put_keys == [cutover.KEY]
    assert "completed" not in json.loads(receipt.read_bytes())


def test_expired_lease_cannot_publish(prepared, monkeypatch):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    monkeypatch.setattr(
        cutover, "verify_held", lambda *_: (_ for _ in ()).throw(ValueError("expired"))
    )
    store.put_keys.clear()
    with pytest.raises(ValueError, match="expired"):
        cutover.publish(store, receipt, handle)
    assert store.put_keys == []


@pytest.mark.parametrize("damage", ["history", "anchor"])
def test_freeze_rejects_lost_history_or_false_anchor(prepared, damage):
    _, _, output, receipt, _ = prepared
    with closing(initialize_database(output)) as conn, conn:
        if damage == "history":
            conn.execute("DELETE FROM economic_source_records WHERE record_count=0")
        else:
            conn.execute("UPDATE economic_source_record_confirmations SET content_sha256='false'")
    with pytest.raises(ValueError, match=r"historical facts|period anchor"):
        cutover.freeze(receipt)
    assert "candidate" not in json.loads(receipt.read_bytes())


def test_changed_frozen_candidate_is_rejected_before_remote_write(prepared):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    value = json.loads(receipt.read_bytes())
    with closing(sqlite3.connect(value["candidate"]["path"])) as conn, conn:
        conn.execute("DELETE FROM economic_source_records")
    store.put_keys.clear()
    with pytest.raises(ValueError, match="frozen cutover upload changed"):
        cutover.publish(store, receipt, handle)
    assert store.put_keys == []


def test_economic_inputs_are_reissued_and_read_back_without_market_or_l2(prepared):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    result = cutover.reissue_inputs(store, receipt, handle)
    assert result["readback_verified"]
    assert "economic-fred_csv" in result["updated"]
    pointer = json.loads(store.get_bytes("coordination/source-inputs/economic-fred_csv.json"))
    assert pointer["canonical_generations"] == {"economic_etag": store.head(cutover.KEY).etag}
    assert not any(key.startswith("lake/") for key in store.head_keys + store.get_keys)
    assert not Path("unused-economic-cutover-market").exists()


def test_frozen_candidate_with_uncheckpointed_wal_is_not_uploaded(prepared):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    candidate = Path(json.loads(receipt.read_bytes())["candidate"]["path"])
    with closing(sqlite3.connect(candidate)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE economic_source_records SET payload=payload")
        conn.commit()
        store.put_keys.clear()
        with pytest.raises(ValueError, match=r"frozen cutover upload changed|pending sidecars"):
            cutover.publish(store, receipt, handle)
        assert store.put_keys == []


def test_input_publication_failure_can_retry_without_overwriting_old_handoff(prepared, monkeypatch):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.put_file
    broken = "coordination/source-inputs/economic-fred_csv.json"

    def fail(target, *args, **kwargs):
        if target == broken:
            raise R2Error("input pointer unavailable")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", fail)
    with pytest.raises(R2Error):
        cutover.reissue_inputs(store, receipt, handle)
    assert "inputs_verified" not in json.loads(receipt.read_bytes())
    monkeypatch.setattr(store, "put_file", original)
    assert cutover.reissue_inputs(store, receipt, handle)["readback_verified"]


def test_cli_dispatches_frozen_publication_and_reports_missing_candidate(
    prepared, monkeypatch, capsys
):
    store, _, _, receipt, handle = prepared
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(cutover, "Boto3R2Store", lambda **_: store)
    assert cutover.main(["publish", "--receipt", str(receipt), "--lease-handle", str(handle)]) == 1
    assert "keep receipt and frozen files" in capsys.readouterr().err
    assert cutover.main(["freeze", "--receipt", str(receipt)]) == 0
    assert cutover.main(["publish", "--receipt", str(receipt), "--lease-handle", str(handle)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["readback_verified"]


@pytest.mark.parametrize("target", [cutover.KEY, cutover.MANIFEST])
@pytest.mark.parametrize("change", ["lease", "peer"])
def test_ownership_change_during_slow_download_cannot_start_new_put(
    prepared, monkeypatch, target, change
):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.download_file
    changed = False

    def slow_download(key, *args, **kwargs):
        nonlocal changed
        result = original(key, *args, **kwargs)
        if (
            key == target
            and not changed
            and (target == cutover.KEY or cutover.KEY in store.put_keys)
        ):
            changed = True
            if change == "lease":
                release(store, _load_handle(handle))
            else:
                store.store_object("runs.sqlite", b"someone else's generation")
        return result

    monkeypatch.setattr(store, "download_file", slow_download)
    store.put_keys.clear()
    with pytest.raises((LeaseError, ValueError)):
        cutover.publish(store, receipt, handle)
    assert changed
    mutable_puts = [key for key in store.put_keys if key in (cutover.KEY, cutover.MANIFEST)]
    assert mutable_puts == ([] if target == cutover.KEY else [cutover.KEY])
    assert "completed" not in json.loads(receipt.read_bytes())


def test_input_handoff_cannot_continue_after_lease_lost_during_body_put(prepared, monkeypatch):
    store, _, _, receipt, handle = prepared
    cutover.freeze(receipt)
    original = store.put_file
    lost = False

    def body_completed(key, *args, **kwargs):
        nonlocal lost
        result = original(key, *args, **kwargs)
        if key.startswith("coordination/source-inputs/") and key.endswith(".sqlite") and not lost:
            lost = True
            release(store, _load_handle(handle))
        return result

    monkeypatch.setattr(store, "put_file", body_completed)
    store.put_keys.clear()
    with pytest.raises(LeaseError):
        cutover.reissue_inputs(store, receipt, handle)
    assert lost
    assert not any(key.endswith("/handoff.json") for key in store.put_keys)
    assert "inputs_verified" not in json.loads(receipt.read_bytes())
    new_handle, _ = acquire(store, "operator")
    new_handle_path = receipt.parent / "resumed-lease.json"
    _save_handle(new_handle_path, new_handle)
    assert cutover.reissue_inputs(store, receipt, new_handle_path)["readback_verified"]


@pytest.mark.parametrize("damage", ["missing", "digest"])
def test_freeze_requires_self_contained_complete_source_confirmation(prepared, damage):
    store, _, output, receipt, _ = prepared
    with closing(initialize_database(output)) as connection, connection:
        if damage == "missing":
            connection.execute("DELETE FROM economic_source_collection_confirmations")
        else:
            connection.execute(
                "UPDATE economic_source_collection_confirmations SET content_sha256='wrong'"
            )
    store.put_keys.clear()
    with pytest.raises(ValueError, match="complete source"):
        cutover.freeze(receipt)
    assert not store.put_keys
    assert "candidate" not in json.loads(receipt.read_bytes())
