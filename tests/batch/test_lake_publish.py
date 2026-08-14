from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.calibration_store import publish_forward, publish_panel

from baibai_batch.storage import lake_publish as lake_publish_module
from baibai_batch.storage.lake_publish import (
    AwsCliR2Store,
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
    publish_calibration_bundle,
    publish_l1_release,
    publish_raw_archive,
    rollback_calibration_bundle,
)
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.keys import current_calibration_bundle_pointer_key
from baibai_engine.market.lake.models import RawArchiveMetadata, load_lake_model_json
from baibai_engine.market.lake.raw import (
    RawRetentionClass,
    archive_raw_file,
    raw_source_ref,
)
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_json_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.writer import capture_legacy_sqlite_snapshot, export_legacy_sqlite
from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.calibration.lake import CalibrationBundlePointer


@dataclass
class _Value:
    body: bytes
    etag: str
    metadata: dict[str, str]
    content_type: str


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, _Value] = {}
        self.conflict_pointer = False
        self.get_keys: list[str] = []

    def head(self, key: str) -> RemoteObject | None:
        value = self.values.get(key)
        if value is None:
            return None
        return RemoteObject(
            etag=value.etag,
            size=len(value.body),
            metadata=value.metadata,
            content_type=value.content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        self.get_keys.append(key)
        return self.values[key].body

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        current = self.values.get(key)
        if "/pointers/" in f"/{key}" and self.conflict_pointer:
            raise LakeCASConflict("injected")
        if if_none_match and current is not None:
            raise LakeCASConflict("exists")
        if if_match is not None and (current is None or current.etag != if_match):
            raise LakeCASConflict("stale")
        body = path.read_bytes()
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()  # nosec B324
        self.values[key] = _Value(
            body=body,
            etag=etag,
            metadata={
                "sha256": sha256,
                "content-md5": content_md5,
                "integrity": "content-md5-v1",
            },
            content_type=content_type,
        )
        result = self.head(key)
        assert result is not None
        return result


def test_calibration_bundle_switches_one_pointer_after_the_complete_graph(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    publish_panel(
        mirror,
        "2026-01-30",
        [{"ticker": "1301", "er_annual": 0.1, "pass_screen": True}],
    )
    publish_forward(
        mirror,
        "2026-01-30",
        [{"ticker": "1301", "horizon": "1y", "status": "unresolved_future_horizon"}],
    )
    local_pointer = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    remote = _MemoryStore()

    report = publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / local_pointer.current.manifest_key,
        store=remote,
    )

    assert report.bundle_id == local_pointer.current.bundle_id
    assert current_calibration_bundle_pointer_key() in remote.values
    assert not any(key.startswith("lake/pointers/l2/") for key in remote.values)
    remote_pointer = CalibrationBundlePointer.model_validate_json(
        remote.values[current_calibration_bundle_pointer_key()].body
    )
    assert remote_pointer.current == local_pointer.current
    assert remote_pointer.previous is None


def test_calibration_bundle_cas_conflict_leaves_the_previous_pointer(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    publish_panel(mirror, "2026-01-30", [])
    local_pointer = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    remote = _MemoryStore()
    remote.conflict_pointer = True

    with pytest.raises(LakeCASConflict):
        publish_calibration_bundle(
            mirror_root=mirror,
            bundle_manifest_path=mirror / local_pointer.current.manifest_key,
            store=remote,
        )

    assert current_calibration_bundle_pointer_key() not in remote.values


def test_calibration_bundle_retry_rejects_a_different_remote_rollback(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    remote = _MemoryStore()
    publish_panel(mirror, "2026-01-30", [])
    first = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / first.current.manifest_key,
        store=remote,
    )
    publish_panel(mirror, "2026-02-27", [])
    second = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / second.current.manifest_key,
        store=remote,
    )
    key = current_calibration_bundle_pointer_key()
    damaged = canonical_json_bytes(CalibrationBundlePointer(current=second.current, previous=None))
    remote.values[key] = _Value(
        body=damaged,
        etag=hashlib.md5(damaged, usedforsecurity=False).hexdigest(),  # nosec B324
        metadata={
            "sha256": hashlib.sha256(damaged).hexdigest(),
            "content-md5": "present",
            "integrity": "content-md5-v1",
        },
        content_type="application/json",
    )

    with pytest.raises(LakePublishError, match="different rollback identity"):
        publish_calibration_bundle(
            mirror_root=mirror,
            bundle_manifest_path=mirror / second.current.manifest_key,
            store=remote,
        )


def test_calibration_bundle_rollback_exchanges_current_and_previous(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    remote = _MemoryStore()
    publish_panel(mirror, "2026-01-30", [])
    first = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / first.current.manifest_key,
        store=remote,
    )
    publish_panel(mirror, "2026-02-27", [])
    second = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / second.current.manifest_key,
        store=remote,
    )

    report = rollback_calibration_bundle(store=remote)

    rolled_back = CalibrationBundlePointer.model_validate_json(
        remote.get_bytes(current_calibration_bundle_pointer_key())
    )
    assert report.bundle_id == first.current.bundle_id
    assert rolled_back.current == first.current
    assert rolled_back.previous == second.current


def test_calibration_bundle_rollback_refuses_an_incomplete_previous_graph(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror"
    remote = _MemoryStore()
    publish_panel(mirror, "2026-01-30", [])
    first = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / first.current.manifest_key,
        store=remote,
    )
    publish_panel(mirror, "2026-02-27", [])
    second = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / second.current.manifest_key,
        store=remote,
    )
    first_bundle = load_lake_model_json(
        remote.get_bytes(first.current.manifest_key), lake_models.CalibrationBundleManifest
    )
    first_dataset = first_bundle.datasets["calibration.panel_diagnostics"]
    first_manifest = load_lake_model_json(
        remote.get_bytes(first_dataset.manifest_key), lake_models.DatasetManifest
    )
    missing = first_manifest.partitions[0].objects[0].key
    remote.values.pop(missing)
    before = remote.get_bytes(current_calibration_bundle_pointer_key())

    with pytest.raises(LakePublishError, match="remote object"):
        rollback_calibration_bundle(store=remote)

    assert remote.get_bytes(current_calibration_bundle_pointer_key()) == before


def test_calibration_bundle_rollback_can_escape_a_broken_current_graph(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    remote = _MemoryStore()
    publish_panel(mirror, "2026-01-30", [])
    first = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / first.current.manifest_key,
        store=remote,
    )
    publish_panel(mirror, "2026-02-27", [])
    second = CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=mirror,
        bundle_manifest_path=mirror / second.current.manifest_key,
        store=remote,
    )
    remote.values.pop(second.current.manifest_key)

    rollback_calibration_bundle(store=remote)

    rolled_back = CalibrationBundlePointer.model_validate_json(
        remote.get_bytes(current_calibration_bundle_pointer_key())
    )
    assert rolled_back.current == first.current
    assert rolled_back.previous == second.current


def test_calibration_acceptance_can_republish_from_a_fresh_local_mirror(
    tmp_path: Path,
) -> None:
    remote = _MemoryStore()
    first_mirror = tmp_path / "first-run"
    publish_panel(first_mirror, "2026-01-30", [])
    first = CalibrationBundlePointer.model_validate_json(
        (first_mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    publish_calibration_bundle(
        mirror_root=first_mirror,
        bundle_manifest_path=first_mirror / first.current.manifest_key,
        store=remote,
    )

    fresh_mirror = tmp_path / "next-run"
    publish_panel(fresh_mirror, "2026-02-27", [])
    fresh = CalibrationBundlePointer.model_validate_json(
        (fresh_mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )
    aligned = CalibrationBundlePointer(current=fresh.current, previous=first.current)
    (fresh_mirror / current_calibration_bundle_pointer_key()).write_bytes(
        lake_models.canonical_lake_model_bytes(aligned)
    )

    publish_calibration_bundle(
        mirror_root=fresh_mirror,
        bundle_manifest_path=fresh_mirror / fresh.current.manifest_key,
        store=remote,
    )

    current = CalibrationBundlePointer.model_validate_json(
        remote.get_bytes(current_calibration_bundle_pointer_key())
    )
    assert current.current == fresh.current
    assert current.previous == first.current


@pytest.fixture(autouse=True)
def _fixed_publication_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lake_publish_module,
        "_utc_now",
        lambda: datetime(2026, 1, 7, tzinfo=UTC),
    )
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
            }
        )
        for item in lake_models.PILOT_RELEASE_POLICY.datasets
    )
    monkeypatch.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        lake_models.PILOT_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
    )


def _add_short_sale_coverage(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-01-31', '2026-02-01T00:00:00+00:00', 1, 'ok', NULL)"""
    )


def _release(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.execute(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES ('2026-01-06', 0, '2026-01-05', '7203', 'Fund', '', '', 0)"""
    )
    _add_short_sale_coverage(connection)
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        snapshot_id="release-snapshot",
    )
    datasets = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot_ref=snapshot.ref,
            build_id=f"build-{index}",
            created_at=datetime(2026, 1, 7, tzinfo=UTC),
        ).manifest_path
        for index, name in enumerate(("jquants.daily_bars", "jquants.short_sale_reports"), start=1)
    ]
    release_path, _ = create_l1_release(
        dataset_manifest_paths=datasets,
        mirror_root=mirror,
        release_id="release-1",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    return mirror, release_path


def test_publish_uploads_immutable_graph_before_current_pointer(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()

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

    assert first.uploaded_objects == 5
    assert second.uploaded_objects == 0
    assert second.reused_objects == 5
    assert not any(key.startswith("lake/build-inputs/") for key in store.values)
    assert "lake/pointers/l1/current.json" in store.values
    assert set(store.get_keys) == {"lake/pointers/l1/current.json"}


def test_pointer_cas_conflict_leaves_current_unchanged(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    original = canonical_json_bytes(
        L1ReleasePointer(
            release_id="release-previous",
            manifest_key="lake/manifests/releases/l1/release-previous.json",
            manifest_sha256="b" * 64,
        )
    )
    store.values["lake/pointers/l1/current.json"] = _Value(
        body=original,
        etag="old-etag",
        metadata={
            "sha256": hashlib.sha256(original).hexdigest(),
            "content-md5": "transport-proof",
            "integrity": "content-md5-v1",
        },
        content_type="application/json",
    )
    store.conflict_pointer = True

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert store.values["lake/pointers/l1/current.json"].body == original


def test_raw_object_and_metadata_publish_idempotently(tmp_path) -> None:
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    mirror = tmp_path / "mirror"
    _, metadata_path, _ = archive_raw_file(
        source_path=source,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-1",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        request_start=date(2026, 1, 1),
        request_end=date(2026, 1, 31),
    )
    store = _MemoryStore()

    first = publish_raw_archive(
        mirror_root=mirror,
        metadata_path=metadata_path,
        store=store,
    )
    second = publish_raw_archive(
        mirror_root=mirror,
        metadata_path=metadata_path,
        store=store,
    )

    assert first.uploaded_objects == 2
    assert second.uploaded_objects == 0
    assert second.reused_objects == 2


def test_r2_account_id_rejects_endpoint_injection() -> None:
    with pytest.raises(LakePublishError, match="32 lowercase hexadecimal"):
        AwsCliR2Store(
            bucket="baibai-stores",
            env={
                "R2_ACCOUNT_ID": "safe@attacker.example/",
                "R2_ACCESS_KEY_ID": "not-logged",
                "R2_SECRET_ACCESS_KEY": "not-logged",
            },
        )


def test_raw_metadata_rejects_a_non_raw_namespace(tmp_path) -> None:
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    _, _, metadata = archive_raw_file(
        source_path=source,
        mirror_root=tmp_path / "mirror",
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-key",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        request_start=date(2026, 1, 1),
        request_end=date(2026, 1, 31),
    )
    payload = metadata.model_dump(mode="json")
    payload["object_key"] = "lake/pointers/l1/current.json"

    with pytest.raises(ValueError, match="value_error"):
        load_lake_model_json(json.dumps(payload), RawArchiveMetadata)


def test_publish_rejects_local_graph_symlinks_outside_mirror(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    metadata_link = mirror / "metadata.json"
    metadata_link.symlink_to(outside)

    with pytest.raises(LakePublishError, match="escapes mirror root"):
        publish_raw_archive(
            mirror_root=mirror,
            metadata_path=metadata_link,
            store=_MemoryStore(),
        )

    with pytest.raises(LakePublishError, match="escapes mirror root"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=metadata_link,
            store=_MemoryStore(),
        )


def test_release_publish_closes_referenced_raw_graph(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.execute(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES ('2026-01-06', 0, '2026-01-05', '7203', 'Fund', '', '', 0)"""
    )
    _add_short_sale_coverage(connection)
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    _, metadata_path, _ = archive_raw_file(
        source_path=source,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-release",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        request_start=date(2026, 1, 1),
        request_end=date(2026, 1, 31),
    )
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        snapshot_id="raw-release-snapshot",
    )
    datasets = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            raw_source_refs=(raw_source_ref(metadata_path),) if index == 1 else (),
            source_snapshot_ref=snapshot.ref,
            build_id=f"raw-build-{index}",
            created_at=datetime(2026, 1, 7, tzinfo=UTC),
        ).manifest_path
        for index, name in enumerate(("jquants.daily_bars", "jquants.short_sale_reports"), start=1)
    ]
    release_path, _ = create_l1_release(
        dataset_manifest_paths=datasets,
        mirror_root=mirror,
        release_id="raw-release-1",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    store = _MemoryStore()

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )

    assert report.uploaded_objects == 7


def test_release_allows_the_same_ingest_id_in_two_dataset_namespaces(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.execute(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES ('2026-01-06', 0, '2026-01-05', '7203', 'Fund', '', '', 0)"""
    )
    _add_short_sale_coverage(connection)
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        snapshot_id="shared-release-snapshot",
    )
    manifests: list[Path] = []
    for index, dataset in enumerate(("jquants.daily_bars", "jquants.short_sale_reports"), start=1):
        source = tmp_path / f"response-{index}.json.gz"
        source.write_bytes(f"provider-original-{index}".encode())
        _, metadata_path, _ = archive_raw_file(
            source_path=source,
            mirror_root=mirror,
            provider="jquants",
            dataset=dataset,
            ingest_id="shared-run",
            suffix=".json.gz",
            retention_class=RawRetentionClass.PRESERVE,
            retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
            request_start=date(2026, 1, 1),
            request_end=date(2026, 1, 31),
        )
        manifests.append(
            export_legacy_sqlite(
                dataset_name=dataset,
                sqlite_path=sqlite_path,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                raw_source_refs=(raw_source_ref(metadata_path),),
                source_snapshot_ref=snapshot.ref,
                build_id=f"shared-build-{index}",
                created_at=datetime(2026, 1, 7, tzinfo=UTC),
            ).manifest_path
        )
    release_path, _ = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="shared-release",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=_MemoryStore(),
    )

    assert report.uploaded_objects == 9


def test_reuse_rejects_remote_integrity_metadata_change(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
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
    store = _MemoryStore()

    with pytest.raises(LakePublishError, match="release and dataset manifest disagree"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert "lake/pointers/l1/current.json" not in store.values


def test_same_release_id_requires_exact_pointer_identity(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    key = "lake/pointers/l1/current.json"
    wrong = canonical_json_bytes(
        L1ReleasePointer(
            release_id="release-1",
            manifest_key="lake/manifests/releases/l1/release-1.json",
            manifest_sha256="f" * 64,
        )
    )
    store.values[key] = _Value(
        body=wrong,
        etag="wrong-pointer",
        metadata={
            "sha256": hashlib.sha256(wrong).hexdigest(),
            "content-md5": "transport-proof",
            "integrity": "content-md5-v1",
        },
        content_type="application/json",
    )

    with pytest.raises(LakePublishError, match="different identity"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )


def test_success_response_with_missing_remote_object_stops_before_pointer(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)

    class DroppingStore(_MemoryStore):
        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            self.values.pop(key)
            return result

    store = DroppingStore()
    with pytest.raises(LakePublishError, match="metadata postcondition"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )
    assert "lake/pointers/l1/current.json" not in store.values


def test_pointer_precondition_rechecks_the_whole_remote_graph(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)

    class DropEarlierStore(_MemoryStore):
        first_key: str | None = None
        puts = 0

        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            if not key.endswith("pointers/l1/current.json"):
                self.puts += 1
                if self.first_key is None:
                    self.first_key = key
                elif self.puts == 3:
                    assert self.first_key is not None
                    self.values.pop(self.first_key)
            return result

    store = DropEarlierStore()
    with pytest.raises(LakePublishError, match="metadata postcondition"):
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
            store=_MemoryStore(),
        )


def test_r2_adapter_classifies_409_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AwsCliR2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    conflict = subprocess.CompletedProcess(
        args=("aws",),
        returncode=1,
        stdout="",
        stderr="ConditionalRequestConflict (409)",
    )
    monkeypatch.setattr(lake_publish_module.subprocess, "run", lambda *args, **kwargs: conflict)
    with pytest.raises(LakeCASConflict):
        store._run("put-object", "--key", "test")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=("aws",), timeout=120)

    monkeypatch.setattr(lake_publish_module.subprocess, "run", timeout)
    with pytest.raises(LakePublishError, match="timed out"):
        store._run("put-object", "--key", "test")
