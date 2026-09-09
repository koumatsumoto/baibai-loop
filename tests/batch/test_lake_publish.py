from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store

from baibai_batch.storage import lake_publish as lake_publish_module
from baibai_batch.storage import publish_market_lake as market_publish_module
from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    PointerPrecondition,
    RemoteObject,
    publish_l1_release,
)
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake import writer as lake_writer_module
from baibai_engine.market.lake.keys import dataset_manifest_key
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_lake_model_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.writer import (
    LakeBuildError,
    export_lake_legacy,
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
)
from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.sqlite.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_engine.market.sqlite.snapshot import create_snapshot


@pytest.fixture(autouse=True)
def _small_pilot_release_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch)


@pytest.fixture(autouse=True)
def _fixed_publication_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 1, 7, tzinfo=UTC)
    monkeypatch.setattr(
        lake_publish_module,
        "_utc_now",
        lambda: fixed_now,
    )
    create_release = market_publish_module.create_lake_l1_release
    monkeypatch.setattr(
        market_publish_module,
        "create_lake_l1_release",
        lambda **kwargs: create_release(created_at=fixed_now, **kwargs),
    )
    export_lake = market_publish_module.export_lake_legacy
    monkeypatch.setattr(
        market_publish_module,
        "export_lake_legacy",
        lambda **kwargs: export_lake(created_at=fixed_now, **kwargs),
    )


def _release(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", "2026-01-05", 1.0, 1.0),),
        short_sale_reports=(("2026-01-06", 0, "2026-01-05", "7203", "Fund", None, None, None, 0),),
        short_sale_coverage=(
            "test:pilot",
            "2026-01-01",
            "2026-01-31",
            "2026-02-01T00:00:00+00:00",
            1,
        ),
    )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id="release-snapshot"
    ) as snapshot:
        datasets = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=f"build-{index}",
                created_at=datetime(2026, 1, 7, tzinfo=UTC),
            ).manifest_path
            for index, name in enumerate(
                ("jquants.daily_bars", "jquants.short_sale_reports"), start=1
            )
        ]
    release_path, _ = create_l1_release(
        dataset_manifest_paths=datasets,
        mirror_root=mirror,
        release_id="release-1",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    return mirror, release_path


def _successor_release(mirror: Path, release_id: str) -> Path:
    release_path, _ = create_l1_release(
        dataset_manifest_paths=sorted((mirror / "lake/manifests/datasets").glob("*/*.json")),
        mirror_root=mirror,
        release_id=release_id,
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    return release_path


def _bind_market_store_to_pointer(path: Path, pointer: L1ReleasePointer) -> None:
    with sqlite3.connect(path) as connection:
        write_lake_store_origin(
            connection,
            LakeStoreOrigin(
                release_id=pointer.release_id,
                release_manifest_sha256=pointer.manifest_sha256,
            ),
        )


def _replace_pointer(store: MemoryR2Store, payload: bytes) -> None:
    store.store_object("lake/pointers/l1/current.json", payload)


def test_publish_uploads_immutable_graph_before_current_pointer(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()

    first = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    second = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )

    assert first.transfers.uploaded_objects == 5
    assert second.transfers.uploaded_objects == 0
    assert second.transfers.reused_objects == 5
    assert not any(key.startswith("lake/build-inputs/") for key in store.values)
    assert "lake/pointers/l1/current.json" in store.values
    # A run that changes nothing moves no immutable object bytes. The mutable pointer
    # is read once to freeze the starting generation and once for exact-retry detection.
    assert second.transfers.downloaded_bytes == 2 * len(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert not any(
        key.startswith("lake/l1/canonical/")
        for key in store.get_keys[len(store.get_keys) - second.transfers.get_requests :]
    )


def test_publication_proves_each_remote_key_once(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    store.head_keys.clear()

    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)

    graph_heads = [key for key in store.head_keys if not key.startswith("lake/pointers/")]
    assert sorted(graph_heads) == sorted(set(graph_heads))
    assert set(graph_heads) == {key for key in store.values if not key.startswith("lake/pointers/")}


def test_pointer_cas_conflict_leaves_current_unchanged(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    original = store.values["lake/pointers/l1/current.json"].body
    successor_path, _ = create_l1_release(
        dataset_manifest_paths=sorted((mirror / "lake/manifests/datasets").glob("*/*.json")),
        mirror_root=mirror,
        release_id="release-2",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    store.conflict_pointer = True

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=successor_path,
            store=store,
        )

    assert store.values["lake/pointers/l1/current.json"].body == original


def test_final_cas_uses_the_starting_pointer_generation(tmp_path: Path) -> None:
    mirror, first_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=first_path, store=store)
    pointer_key = "lake/pointers/l1/current.json"
    initial = store.head(pointer_key)
    assert initial is not None
    second_path = _successor_release(mirror, "release-2")
    third_path = _successor_release(mirror, "release-3")
    publish_l1_release(mirror_root=mirror, release_manifest_path=second_path, store=store)
    successor = store.values[pointer_key].body

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=third_path,
            store=store,
            pointer_precondition=PointerPrecondition(etag=initial.etag),
        )

    assert store.values[pointer_key].body == successor


def test_absent_starting_pointer_uses_if_none_match(tmp_path: Path) -> None:
    mirror, first_path = _release(tmp_path)
    store = MemoryR2Store()
    second_path = _successor_release(mirror, "release-2")
    publish_l1_release(mirror_root=mirror, release_manifest_path=first_path, store=store)
    successor = store.values["lake/pointers/l1/current.json"].body

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=second_path,
            store=store,
            pointer_precondition=PointerPrecondition(etag=None),
        )

    assert store.values["lake/pointers/l1/current.json"].body == successor


def test_exact_target_identity_is_an_idempotent_retry_with_no_put(tmp_path: Path) -> None:
    mirror, first_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=first_path, store=store)
    initial = store.head("lake/pointers/l1/current.json")
    assert initial is not None
    second_path = _successor_release(mirror, "release-2")
    publish_l1_release(mirror_root=mirror, release_manifest_path=second_path, store=store)
    puts_before = len(store.put_keys)

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=second_path,
        store=store,
        pointer_precondition=PointerPrecondition(etag=initial.etag),
    )

    assert report.release_id == "release-2"
    assert len(store.put_keys) == puts_before


def test_release_manifest_change_after_planning_is_refused(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    expected = hashlib.sha256(release_path.read_bytes()).hexdigest()
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-01-08T00:00:00Z"
    release_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LakePublishError, match="changed after its publication was planned"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            expected_release_sha256=expected,
            store=MemoryR2Store(),
        )


def test_export_manifest_replacement_after_release_creation_never_moves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, _ = _release(tmp_path)
    store = MemoryR2Store()
    create_release = market_publish_module.create_lake_l1_release

    def create_then_replace(**kwargs: object) -> tuple[Path, lake_models.ReleaseManifest]:
        path, release = create_release(**kwargs)  # type: ignore[arg-type]
        dataset_name, entry = next(iter(release.datasets.items()))
        manifest_path = mirror / dataset_manifest_key(
            dataset=dataset_name,
            build_id=entry.build_id,
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["created_at"] = "2026-01-08T00:00:00Z"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        return path, release

    monkeypatch.setattr(market_publish_module, "create_lake_l1_release", create_then_replace)
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)

    with pytest.raises(LakePublishError, match="release and dataset manifest disagree"):
        market_publish_module.publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=mirror,
            store=store,
            release_id="manifest-race",
        )

    assert "lake/pointers/l1/current.json" not in store.values


@pytest.mark.parametrize("failure_phase", ["put", "head", "get"])
def test_ambiguous_pointer_switch_reconciles_an_exact_committed_target(
    tmp_path: Path, failure_phase: str
) -> None:
    mirror, release_path = _release(tmp_path)

    class AmbiguousStore(MemoryR2Store):
        armed = False

        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            if self.armed and key == "lake/pointers/l1/current.json":
                raise AssertionError("a pointer PUT may happen only once")
            if key == "lake/pointers/l1/current.json":
                if failure_phase == "put":
                    raise LakePublishError("injected response timeout after commit")
                self.armed = True
            return result

        def head(self, key: str) -> RemoteObject | None:
            if self.armed and failure_phase == "head" and key == "lake/pointers/l1/current.json":
                self.armed = False
                raise LakePublishError("injected HEAD timeout after commit")
            return super().head(key)

        def get_bytes(self, key: str) -> bytes:
            if self.armed and failure_phase == "get" and key == "lake/pointers/l1/current.json":
                self.armed = False
                raise LakePublishError("injected GET timeout after commit")
            return super().get_bytes(key)

    store = AmbiguousStore()

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )

    assert report.release_id == "release-1"
    serving = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert serving.release_id == "release-1"


def test_ambiguous_pointer_switch_reports_a_different_successor_as_conflict(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)
    successor = canonical_lake_model_bytes(
        L1ReleasePointer(
            release_id="concurrent-successor",
            manifest_key="lake/manifests/releases/l1/concurrent-successor.json",
            manifest_sha256="d" * 64,
        )
    )

    class SuccessorStore(MemoryR2Store):
        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            if key == "lake/pointers/l1/current.json":
                _replace_pointer(self, successor)
                raise LakePublishError("injected timeout after another writer won")
            return result

    store = SuccessorStore()

    with pytest.raises(LakeCASConflict, match="different release"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert store.values["lake/pointers/l1/current.json"].body == successor


def test_ambiguous_pointer_switch_reports_unknown_when_it_cannot_read_current(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)

    class UnreadableStore(MemoryR2Store):
        committed = False

        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            if key == "lake/pointers/l1/current.json":
                self.committed = True
                raise LakePublishError("injected response timeout after commit")
            return result

        def head(self, key: str) -> RemoteObject | None:
            if self.committed and key == "lake/pointers/l1/current.json":
                raise LakePublishError("injected reconciliation timeout")
            return super().head(key)

    with pytest.raises(LakePublishError, match="outcome is unknown"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=UnreadableStore(),
        )


def test_low_level_cli_refuses_to_roll_current_back_to_an_old_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror, old_path = _release(tmp_path)
    current_path = _successor_release(mirror, "release-2")
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=old_path, store=store)
    publish_l1_release(mirror_root=mirror, release_manifest_path=current_path, store=store)
    before = store.values["lake/pointers/l1/current.json"].body
    monkeypatch.setattr(lake_publish_module, "Boto3R2Store", lambda **_: store)

    result = lake_publish_module.main(
        ["--mirror", str(mirror), "--release-manifest", str(old_path)]
    )

    assert result == 1
    assert "cannot replace current" in capsys.readouterr().err
    assert store.values["lake/pointers/l1/current.json"].body == before


def test_low_level_cli_allows_the_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(lake_publish_module, "Boto3R2Store", lambda **_: store)

    result = lake_publish_module.main(
        ["--mirror", str(mirror), "--release-manifest", str(release_path)]
    )

    assert result == 0
    serving = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert serving.release_id == "release-1"


def test_low_level_cli_allows_an_exact_target_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    before = store.values["lake/pointers/l1/current.json"].body
    monkeypatch.setattr(lake_publish_module, "Boto3R2Store", lambda **_: store)

    result = lake_publish_module.main(
        ["--mirror", str(mirror), "--release-manifest", str(release_path)]
    )

    assert result == 0
    assert store.values["lake/pointers/l1/current.json"].body == before


def test_low_level_cli_refuses_same_release_id_with_different_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    before = store.values["lake/pointers/l1/current.json"].body
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-01-08T00:00:00Z"
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(lake_publish_module, "Boto3R2Store", lambda **_: store)

    result = lake_publish_module.main(
        ["--mirror", str(mirror), "--release-manifest", str(release_path)]
    )

    assert result == 1
    assert "cannot replace current" in capsys.readouterr().err
    assert store.values["lake/pointers/l1/current.json"].body == before


def test_market_publish_conflicts_if_current_moves_during_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, first_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=first_path, store=store)
    pointer = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    _bind_market_store_to_pointer(tmp_path / "market.sqlite", pointer)
    successor_path = _successor_release(mirror, "concurrent-successor")
    original_export = market_publish_module.export_lake_legacy

    def concurrent_export(**kwargs: object) -> object:
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=successor_path,
            store=store,
        )
        return original_export(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(market_publish_module, "export_lake_legacy", concurrent_export)
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)

    with pytest.raises(LakeCASConflict):
        market_publish_module.publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=mirror,
            store=store,
            release_id="target",
        )

    serving = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert serving.release_id == "concurrent-successor"


def test_first_publication_needs_no_store_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, _ = _release(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)

    report = market_publish_module.publish_market_lake(
        sqlite_path=tmp_path / "market.sqlite",
        mirror_root=mirror,
        store=store,
        release_id="first-publication",
    )

    serving = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert report.release_id == "first-publication"
    assert serving.release_id == "first-publication"
    assert read_lake_store_origin(tmp_path / "market.sqlite") == LakeStoreOrigin(
        release_id="first-publication",
        release_manifest_sha256=serving.manifest_sha256,
    )


def test_origin_is_checked_from_the_same_sealed_snapshot_that_is_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    pointer = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    live_store = tmp_path / "market.sqlite"
    _bind_market_store_to_pointer(live_store, pointer)
    stale_store = tmp_path / "stale-market.sqlite"
    create_snapshot(live_store, stale_store)
    with sqlite3.connect(stale_store) as connection:
        write_lake_store_origin(
            connection,
            LakeStoreOrigin(
                release_id="older-release",
                release_manifest_sha256="0" * 64,
            ),
        )
    real_create_snapshot = lake_writer_module.create_snapshot

    def replace_between_origin_check_and_seal(_source: Path, output: Path) -> int:
        return real_create_snapshot(stale_store, output)

    monkeypatch.setattr(
        lake_writer_module, "create_snapshot", replace_between_origin_check_and_seal
    )
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)
    before = store.values["lake/pointers/l1/current.json"].body

    with pytest.raises(LakeBuildError, match="sealed SQLite store origin differs"):
        market_publish_module.publish_market_lake(
            sqlite_path=live_store,
            mirror_root=mirror,
            store=store,
            release_id="must-not-publish-stale-snapshot",
        )

    assert store.values["lake/pointers/l1/current.json"].body == before


def test_origin_update_failure_reports_that_the_pointer_already_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, _ = _release(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)

    def fail_origin_update(*_: object, **__: object) -> None:
        raise LakeStoreOriginError("disk full")

    monkeypatch.setattr(market_publish_module, "advance_lake_store_origin", fail_origin_update)

    with pytest.raises(LakePublishError, match=r"pointer advanced.*origin did not"):
        market_publish_module.publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=mirror,
            store=store,
            release_id="published-before-local-failure",
        )

    serving = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert serving.release_id == "published-before-local-failure"
    assert read_lake_store_origin(tmp_path / "market.sqlite") is None


def test_pointer_head_get_straddle_fails_its_metadata_identity(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)

    class StraddlingStore(MemoryR2Store):
        armed = False

        def get_bytes(self, key: str) -> bytes:
            if self.armed and key == "lake/pointers/l1/current.json":
                self.values[key].body += b" "
            return super().get_bytes(key)

    store = StraddlingStore()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    store.armed = True

    with pytest.raises(LakePublishError, match="pointer bytes differ"):
        market_publish_module._serving_pointer(store)


@pytest.mark.parametrize("level", ["release", "dataset"])
def test_structurally_valid_mirror_replacement_is_healed_from_remote(
    tmp_path: Path, level: str
) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    pointer = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    _bind_market_store_to_pointer(tmp_path / "market.sqlite", pointer)
    path = (
        release_path
        if level == "release"
        else next((mirror / "lake/manifests/datasets").glob("*/*.json"))
    )
    expected = path.read_bytes()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-01-08T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")

    fixed = market_publish_module._resolve_serving_release(store, mirror, pointer)

    assert fixed.release_id == pointer.release_id
    assert path.read_bytes() == expected


@pytest.mark.parametrize("level", ["release", "dataset"])
def test_remote_manifest_digest_mismatch_stops_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    source_mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(mirror_root=source_mirror, release_manifest_path=release_path, store=store)
    pointer = L1ReleasePointer.model_validate_json(
        store.values["lake/pointers/l1/current.json"].body
    )
    _bind_market_store_to_pointer(tmp_path / "market.sqlite", pointer)
    key = (
        pointer.manifest_key
        if level == "release"
        else next(key for key in store.values if key.startswith("lake/manifests/datasets/"))
    )
    value = store.values[key]
    payload = json.loads(value.body)
    payload["created_at"] = "2026-01-08T00:00:00Z"
    store.store_object(key, json.dumps(payload).encode(), content_type=value.content_type)
    export_called = False

    def unexpected_export(**_: object) -> object:
        nonlocal export_called
        export_called = True
        raise AssertionError("export must not start")

    monkeypatch.setattr(market_publish_module, "export_lake_legacy", unexpected_export)

    with pytest.raises(LakePublishError, match="downloaded lake object digest mismatch"):
        market_publish_module.publish_market_lake(
            sqlite_path=tmp_path / "market.sqlite",
            mirror_root=tmp_path / "fresh-mirror",
            store=store,
        )

    assert export_called is False


def test_market_publish_logs_one_phase_line_without_polluting_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mirror, _ = _release(tmp_path)
    store = MemoryR2Store()
    monkeypatch.setattr(market_publish_module, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(market_publish_module, "lake_verified_git_commit", lambda: "a" * 40)

    assert (
        market_publish_module.main(
            [
                "--sqlite",
                str(tmp_path / "market.sqlite"),
                "--mirror",
                str(mirror),
                "--release-id",
                "timed-release",
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    assert json.loads(output.out)["release_id"] == "timed-release"
    assert output.err.count("lake publish phases:") == 1
    for phase in (
        "base_resolve=",
        "seal_plan_export=",
        "release_create=",
        "local_graph=",
        "remote_closure=",
        "pointer=",
    ):
        assert phase in output.err


def test_r2_account_id_rejects_endpoint_injection() -> None:
    with pytest.raises(LakePublishError, match="32 lowercase hexadecimal"):
        Boto3R2Store(
            bucket="baibai-stores",
            env={
                "R2_ACCOUNT_ID": "safe@attacker.example/",
                "R2_ACCESS_KEY_ID": "not-logged",
                "R2_SECRET_ACCESS_KEY": "not-logged",
            },
        )


def test_publish_rejects_local_graph_symlinks_outside_mirror(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    metadata_link = mirror / "metadata.json"
    metadata_link.symlink_to(outside)

    with pytest.raises(LakePublishError, match="escapes mirror root"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=metadata_link,
            store=MemoryR2Store(),
        )


def test_reuse_rejects_remote_integrity_metadata_change(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    key = next(key for key in store.values if key.endswith(".parquet"))
    value = store.values[key]
    value.metadata["sha256"] = "f" * 64

    with pytest.raises(LakePublishError, match="remote object metadata postcondition"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )


def test_release_dataset_digest_mismatch_never_creates_pointer(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    manifest_path = next((mirror / "lake/manifests/datasets").glob("*/*.json"))
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    store = MemoryR2Store()

    with pytest.raises(LakePublishError, match="release and dataset manifest disagree"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert "lake/pointers/l1/current.json" not in store.values


def test_same_release_id_requires_exact_pointer_identity(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    key = "lake/pointers/l1/current.json"
    wrong = canonical_lake_model_bytes(
        L1ReleasePointer(
            release_id="release-1",
            manifest_key="lake/manifests/releases/l1/release-1.json",
            manifest_sha256="f" * 64,
        )
    )
    store.store_object(key, wrong)

    with pytest.raises(LakePublishError, match="different identity"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )


def test_the_transfer_report_counts_every_request_the_run_made(tmp_path: Path) -> None:
    """The report exists to demonstrate differential cost, so it must not undercount.

    The adapter reads each object back after writing it, to prove the write landed. That
    read is a request this publication made; leaving it out of the figure that is used to
    judge request cost makes the figure argue for its own conclusion.
    """

    mirror, release_path = _release(tmp_path)
    store = MemoryR2Store()

    report = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)

    assert report.transfers.head_requests == len(store.head_keys)
    assert report.transfers.get_requests == len(store.get_keys)


def test_success_response_with_missing_remote_object_stops_before_pointer(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)

    class DroppingStore(MemoryR2Store):
        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            self.values.pop(key)
            return result

    store = DroppingStore()
    with pytest.raises(LakePublishError, match="remote object is missing"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )
    assert "lake/pointers/l1/current.json" not in store.values


def test_local_graph_mutation_after_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, release_path = _release(tmp_path)
    original = lake_publish_module._ensure_immutable
    mutated = False

    def mutate_then_upload(*args: object, **kwargs: object) -> bool:
        nonlocal mutated
        path = kwargs["path"]
        assert isinstance(path, Path)
        if not mutated and path.suffix == ".parquet":
            path.write_bytes(path.read_bytes() + b"mutation")
            mutated = True
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lake_publish_module, "_ensure_immutable", mutate_then_upload)
    with pytest.raises(LakePublishError, match="local upload source differs"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=MemoryR2Store(),
        )


@pytest.mark.parametrize(
    ("code", "status"),
    [("ConditionalRequestConflict", 409), ("PreconditionFailed", 412)],
)
def test_r2_adapter_classifies_conflict_and_timeout(
    monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    conflict = ClientError(
        {
            "Error": {"Code": code, "Message": "secret"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
    with pytest.raises(LakeCASConflict):
        lake_publish_module._raise_r2_error("put-object", conflict)

    class TimeoutClient:
        def head_object(self, **_: object) -> object:
            raise ReadTimeoutError(endpoint_url="https://redacted.invalid")

    monkeypatch.setattr(store, "_client", lambda *_: TimeoutClient())
    with pytest.raises(LakePublishError, match="timed out"):
        store.head("test")


def test_r2_adapter_sends_conditional_put_and_classifies_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    requests: list[dict[str, object]] = []

    class RecordingClient:
        def put_object(self, **kwargs: object) -> object:
            requests.append(kwargs)
            return {"ETag": '"put"'}

        def head_object(self, **_: object) -> object:
            if not requests:
                raise ClientError(
                    {
                        "Error": {"Code": "404", "Message": "credential-must-not-leak"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )
            return {
                "ETag": '"put"',
                "ContentLength": 3,
                "Metadata": {
                    "sha256": "a" * 64,
                    "content-md5": "md5",
                    "integrity": "content-md5-v1",
                },
                "ContentType": "application/json",
            }

    monkeypatch.setattr(store, "_client", lambda *_: RecordingClient())
    assert store.head("missing") is None
    payload = tmp_path / "payload"
    payload.write_bytes(b"one")
    store.put_file(
        "key",
        payload,
        sha256="a" * 64,
        content_md5="md5",
        content_type="application/json",
        if_match="start-etag",
    )

    assert requests[0]["IfMatch"] == "start-etag"
    assert "IfNoneMatch" not in requests[0]
    assert requests[0]["ContentMD5"] == "md5"


@pytest.mark.parametrize("existing", [False, True])
def test_r2_download_stream_failure_never_exposes_partial_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "cache.json"
    if existing:
        target.write_bytes(b"known-good")

    class BrokenBody:
        reads = 0

        def read(self, _: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise ReadTimeoutError(endpoint_url="https://redacted.invalid")

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": BrokenBody()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(LakePublishError, match="timed out"):
        store.download_file("key", target, expect_bytes=10)

    if existing:
        assert target.read_bytes() == b"known-good"
    else:
        assert not target.exists()
    assert list(tmp_path.glob(".cache.json.*.part")) == []


@pytest.mark.parametrize(("payload", "expected"), [(b"short", 6), (b"too-long", 7)])
def test_r2_download_rejects_size_mismatch_without_replacing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    expected: int,
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "cache.json"
    target.write_bytes(b"known-good")

    class Body:
        returned = False

        def read(self, _: int) -> bytes:
            if self.returned:
                return b""
            self.returned = True
            return payload

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": Body()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(LakePublishError, match="size mismatch"):
        store.download_file("key", target, expect_bytes=expected)

    assert target.read_bytes() == b"known-good"


def test_r2_download_atomically_installs_complete_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "nested" / "cache.json"

    class Body:
        chunks = iter((b"complete", b""))

        def read(self, _: int) -> bytes:
            return next(self.chunks)

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": Body()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    store.download_file("key", target, expect_bytes=8)

    assert target.read_bytes() == b"complete"
    assert list(target.parent.glob(".cache.json.*.part")) == []


def test_operation_timeout_covers_the_object_it_transfers() -> None:
    """A per-attempt read timeout must leave enough room for the object."""

    two_gigabytes = 2_013_155_328
    measured_upload_seconds = 181

    assert lake_publish_module._operation_timeout(None) == 120
    assert lake_publish_module._operation_timeout(0) == 120
    assert lake_publish_module._operation_timeout(two_gigabytes) > measured_upload_seconds


def _two_month_release(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A store whose history spans two months, published as one release."""

    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
        [("1301", "2026-01-28", 1.0), ("1301", "2026-02-04", 2.0)],
    )
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES (?, 0, ?, '7203', 'Fund', '', '', 0)""",
        [("2026-01-29", "2026-01-28"), ("2026-02-05", "2026-02-04")],
    )
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-02-28', '2026-03-01T00:00:00+00:00', 2, 'ok', NULL)"""
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    build = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=None,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    release_path, _ = create_l1_release(
        dataset_manifest_paths=sorted(item.manifest_path for item in build.datasets.values()),
        mirror_root=mirror,
        release_id="two-month-1",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    return sqlite_path, mirror, release_path


def test_a_one_month_correction_moves_only_that_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content addressing, end to end: every month is derived again, one month moves."""

    monkeypatch.setattr(lake_publish_module, "_utc_now", lambda: datetime(2026, 3, 2, tzinfo=UTC))
    sqlite_path, mirror, release_path = _two_month_release(tmp_path)
    store = MemoryR2Store()
    first = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    closure_bytes = sum(
        len(value.body)
        for key, value in store.values.items()
        if not key.startswith("lake/pointers/")
    )

    connection = open_connection(sqlite_path)
    connection.execute("UPDATE jquants_daily_bars SET close = 9.0 WHERE traded_at = '2026-02-04'")
    connection.commit()
    connection.close()
    corrected = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=None,
        created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    successor_path, _ = create_l1_release(
        dataset_manifest_paths=sorted(item.manifest_path for item in corrected.datasets.values()),
        mirror_root=mirror,
        release_id="two-month-2",
        created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )

    before = len(store.get_keys)
    second = publish_l1_release(
        mirror_root=mirror, release_manifest_path=successor_path, store=store
    )

    assert corrected.datasets["jquants.daily_bars"].partitions == 2
    assert corrected.datasets["jquants.daily_bars"].new_objects == 1
    # One rewritten Parquet object plus the manifests that name it — not the history.
    assert second.transfers.uploaded_objects == 4
    assert second.transfers.uploaded_bytes < first.transfers.uploaded_bytes
    # Only the month this run rewrote is streamed back. The history's Parquet objects
    # are proved from the identity their immutable write bound to them.
    read_back = [key for key in store.get_keys[before:] if key.endswith(".parquet")]
    assert len(read_back) == 1
    assert second.transfers.downloaded_bytes < closure_bytes
