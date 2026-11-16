"""Finite cutover CAS failure states; no live service or production writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tools.migrations.market_schema_52 import digest, migrate

from baibai_engine.storage.objects.r2 import LakeCASConflict, R2Error, R2OperationStopped
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_jobs.publication import lease
from tools.migrations import publish_market_schema_52 as publish


class Store(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.fault = None
        self.change_after = None
        self.attempts = []
        self.change_on_read = None
        self.verified_reads = {}

    def get_verified_bytes(self, key, *, sha256, max_bytes):
        body = self.get_bytes(key)
        self.verified_reads[key] = self.verified_reads.get(key, 0) + 1
        if self.change_on_read and self.change_on_read[:2] == (key, self.verified_reads[key]):
            self.store_object(self.change_on_read[2], b"changed during final readback")
            self.change_on_read = None
        if len(body) > max_bytes or hashlib.sha256(body).hexdigest() != sha256:
            raise R2Error("fixed read mismatch")
        return body

    def put_file(self, key, path, **kwargs):
        self.attempts.append(key)
        if self.fault and self.fault[0] == key:
            _, mode = self.fault
            self.fault = None
            if mode == "conflict":
                self.store_object(key, b"concurrent writer")
                raise LakeCASConflict("changed")
            if mode in ("403", "429"):
                raise R2OperationStopped("strict rejection " + mode)
            if mode == "before":
                raise R2Error("ack unknown: not received")
            if mode == "after":
                super().put_file(key, path, **kwargs)
                raise R2Error("ack unknown: received")
        result = super().put_file(key, path, **kwargs)
        if self.change_after and self.change_after[0] == key:
            _, changed_key = self.change_after
            self.change_after = None
            self.store_object(changed_key, b"different writer")
        return result


@pytest.fixture
def prepared(schema51, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with sqlite3.connect(schema51) as c:
        write_lake_store_origin(c, LakeStoreOrigin("held-release", "e" * 64))
    target = tmp_path / "target.sqlite"
    migrate(schema51, target, expected_sha256=digest(schema51))
    store = Store()
    store.store_object(publish.MARKET, schema51.read_bytes())
    store.store_object(publish.RUNS, b"canonical generations remain byte-identical")
    pointer = json.dumps({"release_id": "held-release", "manifest_sha256": "e" * 64}).encode()
    store.store_object(publish.POINTER, pointer)
    manifest = json.dumps(
        {
            "schema_version": 1,
            "keys": {
                publish.MARKET: store.values[publish.MARKET].etag,
                publish.RUNS: store.values[publish.RUNS].etag,
            },
        }
    ).encode()
    store.store_object(publish.MANIFEST, manifest)
    handle, _ = lease.acquire(store, "daily")
    plan = publish.Plan(
        format="market-schema-52-cutover-v1",
        source_sha256=digest(schema51),
        target_sha256=digest(target),
        market_etag=store.values[publish.MARKET].etag,
        runs_etag=store.values[publish.RUNS].etag,
        manifest_etag=store.values[publish.MANIFEST].etag,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        pointer_etag=store.values[publish.POINTER].etag,
        pointer_sha256=hashlib.sha256(pointer).hexdigest(),
        written_at_utc=datetime.now(UTC),
    )
    store.attempts.clear()
    store.put_keys.clear()
    return store, dict(
        plan=plan,
        source=schema51,
        target=target,
        handle=handle,
        attempt=tmp_path / "attempt",
        mode="publish",
    )


def finish(args):
    return {**args, "mode": "finish-manifest", "attempt": args["attempt"].with_name("finish")}


def test_success_writes_only_backups_market_and_receipt(prepared):
    store, args = prepared
    fixed = {key: store.values[key] for key in (publish.RUNS, publish.POINTER)}
    files = [(p, digest(p), p.stat().st_mtime_ns) for p in (args["source"], args["target"])]
    result = publish.cutover(store, **args)
    assert result["status"] == "verified_cutover"
    assert result["runs_and_pointer_written"] is False
    assert store.attempts[-2:] == [publish.MARKET, publish.MANIFEST]
    assert len(store.attempts) == 5
    assert all(k.startswith("maintenance/market-schema-52/") for k in store.attempts[:3])
    assert {key: store.values[key] for key in fixed} == fixed
    assert all((digest(p), p.stat().st_mtime_ns) == (sha, mtime) for p, sha, mtime in files)
    assert store.values[publish.MARKET].body == args["target"].read_bytes()
    assert json.loads(store.values[publish.MANIFEST].body)["keys"] == {
        publish.MARKET: store.values[publish.MARKET].etag,
        publish.RUNS: args["plan"].runs_etag,
    }
    # A second explicit finish is read-only, never another market PUT.
    store.attempts.clear()
    assert publish.cutover(store, **finish(args))["status"] == "verified_cutover"
    assert store.attempts == []


def test_attempt_is_never_reused(prepared):
    store, args = prepared
    args["attempt"].mkdir()
    with pytest.raises(ValueError, match="never be overwritten"):
        publish.cutover(store, **args)
    assert store.attempts == []


@pytest.mark.parametrize("kind", ["expired", "replaced"])
def test_lease_is_current_before_any_upload(prepared, kind):
    store, args = prepared
    if kind == "expired":
        handle, _ = lease.acquire(Store(), "daily", now=datetime.now(UTC) - timedelta(hours=4))
        old = lease.PublicationLease(
            "held", handle.owner, handle.purpose, handle.acquired_at_utc, handle.expires_at_utc
        )
        store.store_object(lease.PUBLICATION_LEASE_KEY, lease._encode(old))
        args["handle"] = handle
    else:
        store.store_object(lease.PUBLICATION_LEASE_KEY, b"replaced")
    with pytest.raises(lease.LeaseError):
        publish.cutover(store, **args)
    assert store.attempts == []


@pytest.mark.parametrize("purpose", ["operator", "materialize", "tradingview"])
def test_new_cutover_refuses_other_lease_purposes(prepared, purpose):
    store, args = prepared
    args["handle"] = replace(args["handle"], purpose=purpose)
    with pytest.raises(ValueError, match="current lease"):
        publish.cutover(store, **args)
    assert store.attempts == []


@pytest.mark.parametrize("kind", ["legacy_protocol", "legacy_daily_duration"])
def test_new_cutover_requires_current_daily_handle(prepared, kind):
    store, args = prepared
    handle = args["handle"]
    args["handle"] = (
        replace(handle, protocol_version=1)
        if kind == "legacy_protocol"
        else replace(handle, expires_at_utc=handle.acquired_at_utc + timedelta(minutes=180))
    )
    with pytest.raises(ValueError, match=r"current lease|current daily lease duration"):
        publish.cutover(store, **args)
    assert store.attempts == []


@pytest.mark.parametrize("during_backups", [False, True])
def test_time_budget_is_rechecked_before_canonical_market_write(
    prepared, monkeypatch, during_backups
):
    store, args = prepared
    clock = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(publish, "datetime", Clock)
    short = args["handle"].expires_at_utc - timedelta(minutes=179)
    if during_backups:
        original = publish._backup

        def backup(*a, **kw):
            result = original(*a, **kw)
            if kw["kind"] == "pointer":
                clock[0] = short
            return result

        monkeypatch.setattr(publish, "_backup", backup)
    else:
        clock[0] = short
    with pytest.raises(ValueError, match="insufficient daily lease time"):
        publish.cutover(store, **args)
    assert publish.MARKET not in store.attempts
    assert publish.MANIFEST not in store.attempts
    assert len(store.attempts) == (3 if during_backups else 0)


def test_explicit_finish_can_use_its_own_operator_lease_without_republishing_market(prepared):
    store, args = prepared
    store.fault = publish.MARKET, "after"
    with pytest.raises(R2Error):
        publish.cutover(store, **args)
    lease.release(store, args["handle"])
    handle, _ = lease.acquire(store, "operator")
    store.attempts.clear()
    result = publish.cutover(store, **{**finish(args), "handle": handle})
    assert result["status"] == "verified_cutover"
    assert store.attempts == [publish.MANIFEST]


@pytest.mark.parametrize("changed", [publish.RUNS, publish.POINTER, lease.PUBLICATION_LEASE_KEY])
@pytest.mark.parametrize("when", ["before-market", "after-market"])
def test_boundary_changes_stop_before_next_mutation(prepared, changed, when):
    store, args = prepared
    trigger = (
        publish.MARKET
        if when == "after-market"
        else publish._backup_key("pointer", args["plan"].pointer_sha256)
    )
    store.change_after = trigger, changed
    with pytest.raises((ValueError, lease.LeaseError)):
        publish.cutover(store, **args)
    assert publish.MANIFEST not in store.attempts
    assert store.attempts.count(publish.MARKET) == (1 if when == "after-market" else 0)


@pytest.mark.parametrize("key", [publish.MARKET, publish.MANIFEST])
def test_cas_conflict_never_overwrites_the_competing_body(prepared, key):
    store, args = prepared
    store.fault = key, "conflict"
    with pytest.raises(LakeCASConflict):
        publish.cutover(store, **args)
    assert store.values[key].body == b"concurrent writer"
    assert store.attempts.count(key) == 1
    store.attempts.clear()
    with pytest.raises(R2Error):
        publish.cutover(store, **finish(args))
    assert store.attempts == []


@pytest.mark.parametrize("key", [publish.MARKET, publish.MANIFEST])
@pytest.mark.parametrize("landed", [False, True])
def test_ambiguous_ack_stops_and_only_explicit_exact_finish_can_advance(prepared, key, landed):
    store, args = prepared
    store.fault = key, "after" if landed else "before"
    with pytest.raises(R2Error):
        publish.cutover(store, **args)
    assert store.attempts.count(key) == 1
    if key == publish.MARKET:
        assert publish.MANIFEST not in store.attempts
    store.attempts.clear()
    if key == publish.MARKET and not landed:
        with pytest.raises(R2Error):
            publish.cutover(store, **finish(args))
        assert store.attempts == []
    else:
        assert publish.cutover(store, **finish(args))["status"] == "verified_cutover"
        assert store.attempts == ([] if key == publish.MANIFEST and landed else [publish.MANIFEST])


@pytest.mark.parametrize(
    "key", [publish.RUNS, publish.POINTER, publish.MANIFEST, lease.PUBLICATION_LEASE_KEY]
)
def test_finish_refuses_a_changed_pair_or_lease(prepared, key):
    store, args = prepared
    store.fault = publish.MARKET, "after"
    with pytest.raises(R2Error):
        publish.cutover(store, **args)
    store.store_object(key, b"newer state")
    store.attempts.clear()
    with pytest.raises((ValueError, lease.LeaseError, R2Error)):
        publish.cutover(store, **finish(args))
    assert store.attempts == []


@pytest.mark.parametrize("status", ["403", "429"])
def test_strict_rejection_stops_without_any_followup(prepared, status):
    store, args = prepared
    store.fault = publish.MARKET, status
    with pytest.raises(R2OperationStopped):
        publish.cutover(store, **args)
    assert store.attempts[-1] == publish.MARKET
    assert store.attempts.count(publish.MARKET) == 1
    assert publish.MANIFEST not in store.attempts
    assert (
        "stopped_without_retry:R2OperationStopped" in (args["attempt"] / "events.jsonl").read_text()
    )


def test_original_backup_must_be_readable_before_market_write(prepared):
    store, args = prepared
    store.store_object(publish._backup_key("source", args["plan"].source_sha256), b"bad backup")
    with pytest.raises(R2Error):
        publish.cutover(store, **args)
    assert store.attempts == []


@pytest.mark.parametrize("changed", [publish.RUNS, publish.POINTER, lease.PUBLICATION_LEASE_KEY])
@pytest.mark.parametrize("mode", ["publish", "finish-manifest"])
def test_terminal_body_readback_is_followed_by_control_verification(prepared, changed, mode):
    store, args = prepared
    if mode == "finish-manifest":
        publish.cutover(store, **args)
        args = finish(args)
        store.verified_reads.clear()
    # Publish reads old receipt twice then its new receipt. Idempotent finish reads new twice.
    store.change_on_read = (publish.MANIFEST, 3 if mode == "publish" else 2, changed)
    with pytest.raises((ValueError, lease.LeaseError)):
        publish.cutover(store, **args)
    assert store.change_on_read is None
    assert not (args["attempt"] / "result.json").exists()
    assert "stopped_without_retry:" in (args["attempt"] / "events.jsonl").read_text()


@pytest.mark.parametrize("kind", ["source", "manifest", "pointer"])
def test_backup_identity_is_fixed_by_plan_not_mutable_local_bytes(prepared, kind, monkeypatch):
    store, args = prepared
    original = store.get_verified_bytes
    plan = args["plan"]
    expected_sha = getattr(
        plan,
        {"source": "source_sha256", "manifest": "manifest_sha256", "pointer": "pointer_sha256"}[
            kind
        ],
    )

    def read_and_move_local(key, **kwargs):
        body = original(key, **kwargs)
        if key == publish.MARKET and kwargs["sha256"] == plan.source_sha256:
            local = args["source"] if kind == "source" else args["attempt"] / f"old-{kind}.json"
            local.write_bytes(b"changed local preparation")
        return body

    monkeypatch.setattr(store, "get_verified_bytes", read_and_move_local)
    with pytest.raises(ValueError, match="prepared backup bytes changed"):
        publish.cutover(store, **args)
    assert publish.MARKET not in store.attempts
    assert publish.MANIFEST not in store.attempts
    assert publish._backup_key(kind, expected_sha) not in store.values
    assert not (args["attempt"] / "result.json").exists()
