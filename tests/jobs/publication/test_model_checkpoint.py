import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.model_generation import empty_selection, generation, publish_generation
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_jobs.publication import lease
from baibai_jobs.publication.model_checkpoint import ModelPublicationCheckpoint


@pytest.fixture
def checkpoint(tmp_path):
    store = MemoryR2Store()
    handle, _ = lease.acquire(store, "daily")
    runs = tmp_path / "runs.sqlite"
    old = generation()
    publish_generation(old, path=runs)
    current = old.model_copy(
        update={
            "generation_id": "models-new-release",
            "inputs": {
                "market": InputSnapshot(
                    kind="lake_release", reference="new-release", sha256="a" * 64
                )
            },
        }
    )
    publish_generation(current, path=runs)
    ScreeningSelectionStore(runs).publish("selected", empty_selection(current.generation_id))
    prepared = SimpleNamespace(
        release=SimpleNamespace(release_id="new-release"),
        release_sha256="a" * 64,
        serving=SimpleNamespace(precondition=SimpleNamespace(etag="b" * 32)),
    )
    config = ModelPublicationCheckpoint("c" * 64, tmp_path / "checkpoint", (old.generation_id,))
    args = dict(
        store=store,
        handle=handle,
        prepared=prepared,
        runs=runs,
        generation_id=current.generation_id,
        selection_id="selected",
    )
    return config, args


def test_preserves_full_runs_and_discoverable_exact_receipt_before_pointer(checkpoint):
    config, args = checkpoint
    original = args["runs"].read_bytes()
    config.protect(**args)
    store = args["store"]
    receipt = json.loads(store.get_bytes(config.receipt_key))
    body = store.get_bytes(receipt["runs"]["key"])
    assert receipt["generation_id"] == "models-new-release"
    assert receipt["selection_id"] == "selected"
    assert receipt["expected_previous_pointer_etag"] == "b" * 32
    assert hashlib.sha256(body).hexdigest() == receipt["runs"]["sha256"]
    assert len(body) == receipt["runs"]["bytes"]
    assert args["runs"].read_bytes() == original
    assert not list(config.directory.glob("prepared-runs-*"))
    assert not any("/pointers/" in key for key in store.put_keys)
    with pytest.raises(ValueError, match="already exists"):
        config.protect(**args)


@pytest.mark.parametrize(
    "operation", ["snapshot-put", "snapshot-get", "receipt-put", "receipt-get"]
)
def test_lost_ack_or_corrupted_readback_never_returns_success(checkpoint, monkeypatch, operation):
    config, args = checkpoint
    store = args["store"]
    put = store.put_file
    get = store.download_file
    read = store.get_bytes

    def broken_put(key, *a, **kw):
        result = put(key, *a, **kw)
        if (key.endswith(".sqlite") and operation == "snapshot-put") or (
            key == config.receipt_key and operation == "receipt-put"
        ):
            raise R2Error("lost acknowledgement")
        return result

    def broken_get(key, path, **kw):
        get(key, path, **kw)
        if operation == "snapshot-get":
            path.write_bytes(b"wrong")

    def broken_read(key):
        value = read(key)
        return b"wrong" if key == config.receipt_key and operation == "receipt-get" else value

    monkeypatch.setattr(store, "put_file", broken_put)
    monkeypatch.setattr(store, "download_file", broken_get)
    monkeypatch.setattr(store, "get_bytes", broken_read)
    with pytest.raises((ValueError, LakePublishError)):
        config.protect(**args)
    assert len([key for key in store.put_keys if key.endswith(".sqlite")]) == 1


def test_create_only_conflict_stops_without_replacement(checkpoint, monkeypatch):
    config, args = checkpoint
    store = args["store"]
    put = store.put_file

    def collision(key, *a, **kw):
        if key.endswith(".sqlite"):
            store.store_object(key, b"other")
        return put(key, *a, **kw)

    monkeypatch.setattr(store, "put_file", collision)
    with pytest.raises(LakeCASConflict):
        config.protect(**args)
    assert store.head(config.receipt_key) is None


def test_insufficient_remaining_time_prevents_any_checkpoint_write(checkpoint):
    config, args = checkpoint
    # A valid short operator lease, not a falsified handle for a different remote record.
    store = MemoryR2Store()
    handle, _ = lease.acquire(store, "operator", now=datetime.now(UTC) - timedelta(minutes=110))
    args.update(store=store, handle=handle)
    before = len(store.put_keys)
    with pytest.raises(ValueError, match="insufficient lease time"):
        replace(config, minimum_remaining_seconds=3600).protect(**args)
    assert len(store.put_keys) == before


def test_guard_rechecked_after_durable_upload(checkpoint, monkeypatch):
    config, args = checkpoint
    calls = []
    original = ModelPublicationCheckpoint.guard

    def guard(self, store, handle):
        calls.append(True)
        original(self, store, handle)
        if len(calls) == 2:
            raise ValueError("insufficient lease time")

    monkeypatch.setattr(ModelPublicationCheckpoint, "guard", guard)
    with pytest.raises(ValueError, match="insufficient lease time"):
        config.protect(**args)
    assert args["store"].head(config.receipt_key) is not None
    assert len(calls) == 2


def test_wrong_generation_closure_blocks_artifact_put(checkpoint):
    config, args = checkpoint
    with pytest.raises(ValueError, match="exact generations"):
        replace(config, prior_generation_ids=()).protect(**args)
    assert args["store"].head(config.receipt_key) is None


def test_lease_lost_during_durable_readback_prevents_return(checkpoint, monkeypatch):
    config, args = checkpoint
    store = args["store"]
    download = store.download_file

    def release_during_download(*a, **kw):
        download(*a, **kw)
        lease.release(store, args["handle"])

    monkeypatch.setattr(store, "download_file", release_during_download)
    with pytest.raises(lease.LeaseError):
        config.protect(**args)
    assert store.head(config.receipt_key) is not None


def test_rejection_stops_without_prepared_receipt(checkpoint, monkeypatch):
    from baibai_engine.storage.objects.r2 import R2OperationStopped

    config, args = checkpoint

    def denied(*a, **kw):
        raise R2OperationStopped("HTTP403")

    monkeypatch.setattr(args["store"], "put_file", denied)
    with pytest.raises(R2OperationStopped):
        config.protect(**args)
    assert args["store"].head(config.receipt_key) is None


def test_full_prior_fingerprints_are_checked_before_snapshot_put(checkpoint):
    from baibai_jobs.integration.jpx_recovery_verification import prior_generations

    config, args = checkpoint
    prior = prior_generations(args["runs"], config.prior_generation_ids)
    prior["generations"][config.prior_generation_ids[0]]["generated_model"]["sha256"] = "0" * 64
    full = replace(
        config,
        prior_fingerprints=prior,
        legacy_fingerprints={"screening_run": {}, "stock_analysis": {}},
    )
    with pytest.raises(ValueError, match="prior generation rows changed"):
        full.protect(**args)
    assert not any(key.endswith(".sqlite") for key in args["store"].put_keys)
