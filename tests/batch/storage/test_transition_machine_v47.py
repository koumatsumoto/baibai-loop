"""Bounded machine transition against an in-memory conditional object store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import botocore.session
import pytest
import tools.cutover.transition_machine_v47 as transition_module
from botocore.awsrequest import AWSRequest
from botocore.hooks import HierarchicalEmitter
from tests.helpers.l1_release import lake, market_store  # noqa: F401 - fixture import
from tests.helpers.r2_store import MemoryR2Store
from tools.cutover.compact_market_v47 import build_compact_market
from tools.cutover.publish_fresh_market_v47 import OLD_POINTER_KEY, SourcePin
from tools.cutover.transition_machine_v47 import (
    APPLICATION_KEY,
    ECONOMIC_KEY,
    MARKET_KEY,
    PRODUCTION_MACHINE,
    RECEIPT_KEY,
    RUNS_KEY,
    CutoverError,
    MachinePin,
    R2MachineStore,
    required_compact_workspace_bytes,
    required_workspace_bytes,
    transition_machine,
)

from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError, RemoteObject
from baibai_batch.storage.publication_lease import LeaseError, acquire, release
from baibai_engine.collection.economic.store import EconomicSchemaError, initialize_database
from baibai_engine.storage.lake.objects import LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
)


def _wire(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_pinned_economic_sha256_is_complete() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", PRODUCTION_MACHINE.economic_sha256)


def test_space_bound_includes_market_vacuum_before_economic_snapshot() -> None:
    assert required_workspace_bytes(1_000, 200) == 3_000 + 1024**3
    assert required_workspace_bytes(100, 500) == 600 + 1024**3


def test_compact_bound_is_separate_and_uses_measured_output() -> None:
    assert required_compact_workspace_bytes(100, 500) == 700 + 1024**3


class FakeMachineStore(MemoryR2Store):
    def __init__(self) -> None:
        super().__init__()
        self.uncertain_market = False
        self.lose_lease_after_market = None
        self.conflict_receipt = False
        self.copy_keys: list[tuple[str, str]] = []

    def sqlite_user_version(self, key: str, *, etag: str) -> int:
        assert self.head(key).etag == etag  # type: ignore[union-attr]
        return {MARKET_KEY: 39, ECONOMIC_KEY: 9}[key]

    def copy_if_match(self, source: str, backup: str, *, etag: str):  # type: ignore[no-untyped-def]
        self.copy_keys.append((source, backup))
        if self.head(source).etag != etag or self.head(backup) is not None:  # type: ignore[union-attr]
            raise LakeCASConflict("backup CAS")
        old = self.values[source]
        self.store_object(backup, old.body, content_type=old.content_type)
        return self.head(backup)

    def put_file(self, key: str, path: Path, **kwargs):  # type: ignore[no-untyped-def]
        if key == RECEIPT_KEY and self.conflict_receipt:
            self.store_object(RECEIPT_KEY, b'{"competing":true}\n')
        result = super().put_file(key, path, **kwargs)
        if key == MARKET_KEY and self.lose_lease_after_market is not None:
            self.lose_lease_after_market()
        if key == MARKET_KEY and self.uncertain_market:
            raise LakePublishError("timeout after market PUT committed")
        return result


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = FakeMachineStore()
    lease, _ = acquire(store, "operator")
    release_id = "20261019T000000Z-fresh"
    digest = "a" * 64
    market = market_store(tmp_path / "fresh.sqlite")
    advance_lake_store_origin(
        market,
        expected=None,
        target=LakeStoreOrigin(release_id=release_id, release_manifest_sha256=digest),
    )
    economic = tmp_path / "economic.sqlite"
    with closing(initialize_database(economic)) as connection, connection:
        connection.execute(
            "INSERT INTO economic_source_records "
            "(source_id, observed_at, fetched_at, record_count, payload) "
            "VALUES ('fixture', '2026-10-19', '2026-10-19T00:00:00Z', 1, x'01')"
        )
    old_pointer = _wire({"release_id": "old"})
    store.store_object(OLD_POINTER_KEY, old_pointer)
    source = SourcePin(
        pointer_etag=store.head(OLD_POINTER_KEY).etag,  # type: ignore[union-attr]
        pointer_sha256=_sha(old_pointer),
        release_id="old",
        manifest_sha256="b" * 64,
        market_etag="unused",
        market_bytes=0,
        market_schema=39,
        contracts={},
    )
    pointer = _wire(
        {
            "pointer_version": 1,
            "release_id": release_id,
            "manifest_key": f"lake/manifests/releases/market/{release_id}.json",
            "manifest_sha256": digest,
        }
    )
    store.store_object("lake/pointers/market/current.json", pointer)
    for key in (MARKET_KEY, ECONOMIC_KEY, RUNS_KEY, APPLICATION_KEY):
        store.store_object(key, f"old-{key}".encode(), content_type="application/octet-stream")
    old_etags = {
        key: store.head(key).etag for key in (MARKET_KEY, ECONOMIC_KEY, RUNS_KEY, APPLICATION_KEY)
    }  # type: ignore[union-attr]
    receipt = _wire(
        {
            "schema_version": 1,
            "written_at_utc": "2026-10-19T00:00:00Z",
            "keys": {
                MARKET_KEY: old_etags[MARKET_KEY],
                RUNS_KEY: old_etags[RUNS_KEY],
                ECONOMIC_KEY: old_etags[ECONOMIC_KEY],
            },
        }
    )
    store.store_object(RECEIPT_KEY, receipt)
    pin = MachinePin(
        market_etag=old_etags[MARKET_KEY],
        economic_etag=old_etags[ECONOMIC_KEY],
        runs_etag=old_etags[RUNS_KEY],
        application_etag=old_etags[APPLICATION_KEY],
        receipt_etag=store.head(RECEIPT_KEY).etag,  # type: ignore[union-attr]
        receipt_sha256=_sha(receipt),
        market_sha256=_sha(store.values[MARKET_KEY].body),
        economic_sha256=_sha(store.values[ECONOMIC_KEY].body),
    )
    source = SourcePin(
        pointer_etag=source.pointer_etag,
        pointer_sha256=source.pointer_sha256,
        release_id=source.release_id,
        manifest_sha256=source.manifest_sha256,
        market_etag=pin.market_etag,
        market_bytes=0,
        market_schema=39,
        contracts={},
    )
    args = dict(
        candidate_market=market,
        candidate_economic=economic,
        stage_dir=tmp_path / "stage",
        mirror=tmp_path / "mirror",
        store=store,
        lease=lease,
        release_id=release_id,
        manifest_sha256=digest,
        transition_id="test-one",
        pin=pin,
        source=source,
        prepare_market=lambda path: None,
    )
    return store, lease, pin, args


def test_two_key_transition_preserves_runs_and_application(tmp_path: Path) -> None:
    store, _, pin, args = _setup(tmp_path)
    before = {key: store.values[key].body for key in (RUNS_KEY, APPLICATION_KEY)}
    result = transition_machine(**args)
    assert len(store.copy_keys) == 2
    assert {key: store.values[key].body for key in before} == before
    assert RUNS_KEY not in store.put_keys
    assert APPLICATION_KEY not in store.put_keys
    assert json.loads(store.values[RECEIPT_KEY].body)["keys"] == result["new_etags"]
    assert store.values[MARKET_KEY].etag != pin.market_etag
    assert store.values[ECONOMIC_KEY].etag != pin.economic_etag
    assert set(result["backup_keys"]) == {MARKET_KEY, ECONOMIC_KEY}


def test_market_preparation_precedes_economic_snapshot(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)

    def prepare_market(path: Path) -> None:
        assert path.is_file()
        assert not (args["stage_dir"] / "test-one-indicators.sqlite").exists()

    args["prepare_market"] = prepare_market
    transition_machine(**args)
    assert store.head(ECONOMIC_KEY) is not None


def test_uncertain_committed_market_put_is_reconciled(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)
    store.uncertain_market = True
    result = transition_machine(**args)
    assert result["new_etags"][MARKET_KEY] == store.head(MARKET_KEY).etag  # type: ignore[union-attr]
    assert store.put_keys.count(MARKET_KEY) == 1


def test_lease_loss_after_first_put_stops_before_second(tmp_path: Path) -> None:
    store, lease, pin, args = _setup(tmp_path)
    store.lose_lease_after_market = lambda: release(store, lease)
    with pytest.raises(LeaseError):
        transition_machine(**args)
    assert store.head(MARKET_KEY).etag != pin.market_etag  # type: ignore[union-attr]
    assert store.head(ECONOMIC_KEY).etag == pin.economic_etag  # type: ignore[union-attr]
    assert store.head(RECEIPT_KEY).etag == pin.receipt_etag  # type: ignore[union-attr]


def test_receipt_conflict_stops_with_partial_state_visible(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)
    store.conflict_receipt = True
    with pytest.raises(CutoverError, match="unverified"):
        transition_machine(**args)
    assert store.put_keys.count(MARKET_KEY) == 1
    assert store.put_keys.count(ECONOMIC_KEY) == 1


def test_space_guard_precedes_any_full_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _, args = _setup(tmp_path)

    class Tiny:
        f_bavail = 1
        f_frsize = 1

    monkeypatch.setattr(os, "statvfs", lambda path: Tiny())
    with pytest.raises(CutoverError, match="insufficient staging space"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_changed_runs_generation_stops_before_writes(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)
    store.store_object(RUNS_KEY, b"different")
    with pytest.raises(CutoverError, match=r"pinned generation changed: runs\.sqlite"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_wrong_economic_shape_with_current_version_stops_before_writes(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)
    with sqlite3.connect(args["candidate_economic"]) as connection:
        connection.execute("DROP TABLE economic_source_documents")
    with pytest.raises(EconomicSchemaError, match="table contract mismatch"):
        transition_machine(**args)
    assert not store.copy_keys


def test_uncheckpointed_economic_wal_stops_before_writes(tmp_path: Path) -> None:
    store, _, _, args = _setup(tmp_path)
    connection = sqlite3.connect(args["candidate_economic"])
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO economic_source_records "
            "(source_id, observed_at, fetched_at, record_count, payload) "
            "VALUES ('wal', '2026-10-19', '2026-10-19T00:00:01Z', 1, x'02')"
        )
        connection.commit()
        with pytest.raises(CutoverError, match="uncheckpointed WAL"):
            transition_machine(**args)
        assert not store.copy_keys
    finally:
        connection.close()


def test_economic_change_during_snapshot_stops_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _, args = _setup(tmp_path)
    original = transition_module.create_snapshot

    def changing_snapshot(source: Path, output: Path) -> int:
        result = original(source, output)
        if source == args["candidate_economic"]:
            with sqlite3.connect(source) as connection:
                connection.execute(
                    "INSERT INTO economic_source_records "
                    "(source_id, observed_at, fetched_at, record_count, payload) "
                    "VALUES ('later', '2026-10-19', '2026-10-19T00:00:02Z', 1, x'03')"
                )
        return result

    monkeypatch.setattr(transition_module, "create_snapshot", changing_snapshot)
    with pytest.raises(CutoverError, match="source candidates changed during staging"):
        transition_machine(**args)
    assert not store.copy_keys


def test_real_dehydration_empties_only_published_market_rows(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    store, _, _, args = _setup(tmp_path)
    mirror, pointer = request.getfixturevalue("lake")
    candidate = tmp_path / "market.sqlite"
    advance_lake_store_origin(
        candidate,
        expected=None,
        target=LakeStoreOrigin(pointer.release_id, pointer.manifest_sha256),
    )
    store.store_object(
        "lake/pointers/market/current.json",
        (mirror / "lake/pointers/market/current.json").read_bytes(),
    )
    args.update(
        candidate_market=candidate,
        mirror=mirror,
        release_id=pointer.release_id,
        manifest_sha256=pointer.manifest_sha256,
        prepare_market=None,
    )
    transition_machine(**args)
    with sqlite3.connect(args["stage_dir"] / "test-one-market.sqlite") as staged:
        assert staged.execute("SELECT count(*) FROM jquants_daily_bars").fetchone()[0] == 0
    with sqlite3.connect(candidate) as original:
        assert original.execute("SELECT count(*) FROM jquants_daily_bars").fetchone()[0] == 4


def _compact_case(
    tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[FakeMachineStore, dict[str, object]]:
    store, _, _, args = _setup(tmp_path)
    mirror, pointer = request.getfixturevalue("lake")
    candidate = tmp_path / "market.sqlite"
    advance_lake_store_origin(
        candidate,
        expected=None,
        target=LakeStoreOrigin(pointer.release_id, pointer.manifest_sha256),
    )
    store.store_object(
        "lake/pointers/market/current.json",
        (mirror / "lake/pointers/market/current.json").read_bytes(),
    )
    args.update(
        candidate_market=candidate,
        mirror=mirror,
        release_id=pointer.release_id,
        manifest_sha256=pointer.manifest_sha256,
        prepare_market=None,
        market_mode="compact",
    )
    return store, args


def test_compact_transition_uses_fixed_release_without_full_market_snapshot(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, args = _compact_case(tmp_path, request)
    original = transition_module.create_snapshot

    def economic_only(source: Path, output: Path) -> int:
        assert source == args["candidate_economic"]
        return original(source, output)

    monkeypatch.setattr(transition_module, "create_snapshot", economic_only)
    result = transition_machine(**args)
    market_copy = args["stage_dir"] / "test-one-market.sqlite"
    assert result["space"]["measured_compact_bytes"] == market_copy.stat().st_size
    assert result["space"]["initial_bound_bytes"] < required_workspace_bytes(
        args["candidate_market"].stat().st_size,  # type: ignore[union-attr]
        args["candidate_economic"].stat().st_size,  # type: ignore[union-attr]
    )
    with sqlite3.connect(market_copy) as staged:
        assert staged.execute("SELECT count(*) FROM jquants_daily_bars").fetchone() == (0,)
        assert staged.execute("SELECT count(*) FROM source_coverage").fetchone()[0] > 0
    assert store.put_keys.count(MARKET_KEY) == 1


def test_compact_refuses_origin_null_review_artifact_before_remote_mutation(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    store, args = _compact_case(tmp_path, request)
    with sqlite3.connect(args["candidate_market"]) as conn:
        conn.execute("DELETE FROM lake_store_origin")
    with pytest.raises(CutoverError, match="candidate market origin"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_compact_builder_itself_rejects_origin_null(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    mirror, pointer = request.getfixturevalue("lake")
    fixed = resolve_release(
        LocalMirrorSource(mirror),
        pointer.release_id,
        manifest_sha256=pointer.manifest_sha256,
    )
    output = tmp_path / "unpublishable-compact.sqlite"
    with pytest.raises(ValueError, match="compact source origin"):
        build_compact_market(tmp_path / "market.sqlite", output, release=fixed)
    assert not output.exists()


def test_compact_refuses_wrong_published_origin_before_remote_mutation(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    store, args = _compact_case(tmp_path, request)
    with sqlite3.connect(args["candidate_market"]) as conn:
        conn.execute(
            "UPDATE lake_store_origin SET release_manifest_sha256=? WHERE singleton=1",
            ("f" * 64,),
        )
    with pytest.raises(CutoverError, match="candidate market origin"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_compact_refuses_missing_published_source_row(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    store, args = _compact_case(tmp_path, request)
    with sqlite3.connect(args["candidate_market"]) as conn:
        conn.execute("DELETE FROM jquants_daily_bars WHERE ticker='7203'")
    with pytest.raises(ValueError, match="fixed release publishes"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_compact_refuses_unpublished_source_row(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    store, args = _compact_case(tmp_path, request)
    with sqlite3.connect(args["candidate_market"]) as conn:
        conn.execute(
            "INSERT INTO jquants_fin_summaries(ticker,disclosed_at,disclosure_number,"
            "document_payload) VALUES ('1301','2026-02-01','extra','{}')"
        )
    with pytest.raises(ValueError, match="absent from the fixed release"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_compact_detects_source_change_during_staging(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, args = _compact_case(tmp_path, request)
    original = transition_module.build_compact_market

    def changing_compact(source: Path, output: Path, **kwargs: object) -> dict[str, object]:
        result = original(source, output, **kwargs)  # type: ignore[arg-type]
        with sqlite3.connect(source) as conn:
            conn.execute(
                "INSERT INTO source_coverage(source,coverage_key,fetched_at_utc,record_count) "
                "VALUES ('later','new','2026-10-19T00:00:00Z',0)"
            )
        return result

    monkeypatch.setattr(transition_module, "build_compact_market", changing_compact)
    with pytest.raises(CutoverError, match="source candidates changed during staging"):
        transition_machine(**args)
    assert not store.copy_keys
    assert not any(key in (MARKET_KEY, ECONOMIC_KEY, RECEIPT_KEY) for key in store.put_keys)


def test_r2_copy_uses_cloudflare_destination_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = (
        botocore.session.get_session()
        .get_service_model("s3")
        .operation_model("CopyObject")
        .input_shape
    )
    assert shape.members["CopySourceIfMatch"].serialization["name"] == "x-amz-copy-source-if-match"
    assert shape.members["IfNoneMatch"].serialization["name"] == "If-None-Match"

    class Client:
        def __init__(self) -> None:
            self.meta = type("Meta", (), {"events": HierarchicalEmitter()})()
            self.headers: dict[str, str] = {}

        def copy_object(self, **kwargs: object) -> None:
            assert kwargs["CopySourceIfMatch"] == "source-etag"
            assert "IfNoneMatch" not in kwargs
            request = AWSRequest(method="PUT", url="https://example.test/backup")
            self.meta.events.emit("before-sign.s3.CopyObject", request=request)
            self.headers = dict(request.headers)

    client = Client()
    store = R2MachineStore(
        bucket="test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "test",
            "R2_SECRET_ACCESS_KEY": "test",
        },
    )
    monkeypatch.setattr(store, "_client", lambda: client)
    monkeypatch.setattr(
        store,
        "head",
        lambda key: RemoteObject(
            etag="backup-etag", size=1, metadata={"sha256": "a" * 64}, content_type="binary"
        ),
    )
    store.copy_if_match("source", "backup", etag="source-etag")
    assert client.headers["cf-copy-destination-if-none-match"] == "*"
