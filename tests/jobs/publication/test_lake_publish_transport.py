from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import R2Error, RemoteObject
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release


def monthly_release(tmp_path: Path, *, months: int = 36) -> tuple[Path, Path]:
    rows = tuple(
        ("1301", f"{1996 + i // 12:04}-{i % 12 + 1:02}-05", float(i + 1), 1000.0)
        for i in range(months)
    )
    database = market_store(tmp_path / "market.sqlite", daily_bars=rows, short_sale_reports=())
    mirror = tmp_path / "mirror"
    created = datetime(2026, 1, 7, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=database, mirror_root=mirror, snapshot_id="transport-seal"
    ) as snapshot:
        manifest = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.daily_bars"],
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="transport-build",
            created_at=created,
        ).manifest_path
    release, _ = create_release(
        dataset_manifest_paths=[manifest],
        mirror_root=mirror,
        release_id="transport-release",
        created_at=created,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    return mirror, release


@pytest.fixture(autouse=True)
def _policy(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow_release_policy(monkeypatch, datasets=("jquants.daily_bars",))


def test_only_heads_overlap_and_every_request_and_byte_remains_accounted(tmp_path, capsys):
    mirror, release = monthly_release(tmp_path)
    owner = threading.get_ident()

    class Store(MemoryR2Store):
        active = 0
        peak = 0
        lock = threading.Lock()

        def head(self, key: str) -> RemoteObject | None:
            if threading.get_ident() == owner:
                return super().head(key)
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.003)
                return super().head(key)
            finally:
                with self.lock:
                    self.active -= 1

        def put_file(self, key, path, **kwargs):
            assert threading.get_ident() == owner
            return super().put_file(key, path, **kwargs)

        def download_file(self, key, path, *, expect_bytes=None):
            assert threading.get_ident() == owner
            super().download_file(key, path, expect_bytes=expect_bytes)

    store = Store()
    first = publish_release(mirror_root=mirror, release_manifest_path=release, store=store)
    assert first.transfers.uploaded_objects == 38
    assert 1 < store.peak <= 8
    assert first.transfers.head_requests == len(store.head_keys)
    assert first.transfers.get_requests == len(store.get_keys)
    immutable = set(store.values) - {"lake/pointers/market/current.json"}
    assert all(store.head_keys.count(key) == 2 for key in immutable)
    assert all(store.get_keys.count(key) == 1 for key in immutable)
    originals = dict(store.values)
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()

    second = publish_release(mirror_root=mirror, release_manifest_path=release, store=store)
    assert second.transfers.uploaded_objects == 0
    assert second.transfers.reused_objects == 38
    assert second.transfers.head_requests == len(store.head_keys)
    assert second.transfers.get_requests == len(store.get_keys)
    assert all(store.head_keys.count(key) == 1 for key in immutable)
    assert not immutable.intersection(store.get_keys)
    assert store.put_keys == []
    assert store.values == originals
    output = capsys.readouterr()
    assert output.out == ""
    for phase in ("local_graph", "remote_closure", "pointer"):
        assert output.err.count(f"lake publication: phase={phase} state=start") == 2
        assert output.err.count(f"lake publication: phase={phase} state=done") == 2


def test_failed_readback_never_switches_pointer_and_same_input_retry_reuses_objects(
    tmp_path, capsys
):
    mirror, release = monthly_release(tmp_path)

    class Store(MemoryR2Store):
        failed = False

        def download_file(self, key, path, *, expect_bytes=None):
            if not self.failed and len(self.put_keys) == 10:
                self.failed = True
                raise R2Error("injected readback failure")
            super().download_file(key, path, expect_bytes=expect_bytes)

    store = Store()
    guard_calls = []
    with pytest.raises(R2Error, match="injected readback failure"):
        publish_release(
            mirror_root=mirror,
            release_manifest_path=release,
            store=store,
            before_pointer_switch=lambda: guard_calls.append(True),
        )
    assert "lake/pointers/market/current.json" not in store.values
    assert guard_calls == []
    existing = dict(store.values)
    output = capsys.readouterr().err
    assert "phase=remote_closure state=start" in output
    assert "phase=remote_closure state=done" not in output
    assert "phase=pointer" not in output

    result = publish_release(
        mirror_root=mirror,
        release_manifest_path=release,
        store=store,
        before_pointer_switch=lambda: guard_calls.append(True),
    )
    assert result.transfers.reused_objects == 10
    assert result.transfers.uploaded_objects == 28
    assert guard_calls == [True]
    assert all(store.values[key] == value for key, value in existing.items())
