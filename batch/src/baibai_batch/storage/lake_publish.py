"""Publish validated lake graphs to R2, switching their mutable pointer last by CAS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from baibai_engine.batch_api import (
    CalibrationBundleManifest,
    CalibrationBundlePointer,
    CalibrationBundleRef,
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    L1ReleasePointer,
    LakeDatasetManifest,
    LakeRawArchiveMetadata,
    LakeRawIngestSourceRef,
    LakeReleaseManifest,
    LakeSQLiteSnapshotSourceRef,
    canonical_json_bytes,
    canonical_manifest_bytes,
    lake_current_calibration_bundle_pointer_key,
    lake_current_l1_pointer_key,
    lake_dataset_manifest_key,
    lake_release_manifest_key,
    load_lake_model_json,
    resolve_lake_source_ref,
    validate_lake_release_policy,
)

_R2_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_POINTER_BYTES = 64 * 1024


class LakePublishError(RuntimeError):
    pass


class LakeCASConflict(LakePublishError):
    pass


@dataclass(frozen=True)
class RemoteObject:
    etag: str
    size: int
    metadata: Mapping[str, str]
    content_type: str


class ObjectStore(Protocol):
    def head(self, key: str) -> RemoteObject | None: ...

    def get_bytes(self, key: str) -> bytes: ...

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
    ) -> RemoteObject: ...


@dataclass(frozen=True)
class PublishReport:
    release_id: str
    uploaded_objects: int
    reused_objects: int
    pointer_etag: str


@dataclass(frozen=True)
class CalibrationBundlePublishReport:
    bundle_id: str
    uploaded_objects: int
    reused_objects: int
    pointer_etag: str


@dataclass(frozen=True)
class RawPublishReport:
    ingest_id: str
    uploaded_objects: int
    reused_objects: int


def publish_raw_archive(
    *,
    mirror_root: Path,
    metadata_path: Path,
    store: ObjectStore,
) -> RawPublishReport:
    root = mirror_root.resolve()
    resolved_metadata_path = metadata_path.resolve()
    if not resolved_metadata_path.is_relative_to(root):
        raise LakePublishError("Raw metadata path escapes mirror root")
    metadata = load_lake_model_json(resolved_metadata_path.read_bytes(), LakeRawArchiveMetadata)
    object_path = _mirror_path(mirror_root, metadata.object_key)
    expected_metadata_path = Path(f"{object_path}.metadata.json")
    if resolved_metadata_path != expected_metadata_path:
        raise LakePublishError("Raw metadata path does not match its object_key")
    if _sha256(object_path) != metadata.content_sha256:
        raise LakePublishError("Raw object checksum does not match its metadata")
    if object_path.stat().st_size != metadata.bytes:
        raise LakePublishError("Raw object size does not match its metadata")
    metadata_payload = resolved_metadata_path.read_bytes()
    uploads = (
        (
            metadata.object_key,
            object_path,
            "application/octet-stream",
            metadata.content_sha256,
            metadata.bytes,
        ),
        (
            f"{metadata.object_key}.metadata.json",
            resolved_metadata_path,
            "application/json",
            hashlib.sha256(metadata_payload).hexdigest(),
            len(metadata_payload),
        ),
    )
    uploaded = 0
    reused = 0
    for key, path, content_type, expected_sha256, expected_size in uploads:
        if _ensure_immutable(
            store,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        ):
            uploaded += 1
        else:
            reused += 1
    return RawPublishReport(
        ingest_id=metadata.ingest_id,
        uploaded_objects=uploaded,
        reused_objects=reused,
    )


def publish_l1_release(
    *,
    mirror_root: Path,
    release_manifest_path: Path,
    store: ObjectStore,
) -> PublishReport:
    """Upload immutable graph nodes, then atomically switch the one mutable pointer."""
    root = mirror_root.resolve()
    resolved_release_path = release_manifest_path.resolve()
    if not resolved_release_path.is_relative_to(root):
        raise LakePublishError("release manifest path escapes mirror root")
    release_payload = resolved_release_path.read_bytes()
    release = load_lake_model_json(release_payload, LakeReleaseManifest)
    expected_release_key = lake_release_manifest_key(release_id=release.release_id)
    expected_release_path = _mirror_path(mirror_root, expected_release_key)
    if resolved_release_path != expected_release_path:
        raise LakePublishError("release manifest path does not match its release_id")

    uploads: list[tuple[str, Path, str, str, int]] = []
    manifests: dict[str, LakeDatasetManifest] = {}
    referenced_sources: dict[tuple[str, str, str], LakeRawIngestSourceRef] = {}
    for dataset_name, release_dataset in sorted(release.datasets.items()):
        manifest_key = lake_dataset_manifest_key(
            dataset=dataset_name,
            build_id=release_dataset.build_id,
        )
        manifest_path = _mirror_path(mirror_root, manifest_key)
        manifest_payload = manifest_path.read_bytes()
        manifest = load_lake_model_json(manifest_payload, LakeDatasetManifest)
        if (
            manifest.dataset != dataset_name
            or manifest.build_id != release_dataset.build_id
            or manifest.contract_version != release_dataset.contract_version
            or hashlib.sha256(manifest_payload).hexdigest() != release_dataset.manifest_sha256
        ):
            raise LakePublishError(f"release and dataset manifest disagree: {dataset_name}")
        manifests[dataset_name] = manifest
        for partition in manifest.partitions:
            for source in partition.sources:
                if not isinstance(
                    source,
                    (LakeRawIngestSourceRef, LakeSQLiteSnapshotSourceRef),
                ):
                    raise LakePublishError("L1 graph contains an unsupported source reference")
                resolve_lake_source_ref(mirror_root, source)
                if isinstance(source, LakeRawIngestSourceRef):
                    referenced_sources[(source.kind, source.key, source.sha256)] = source
            for item in partition.objects:
                object_path = _mirror_path(mirror_root, item.key)
                if _sha256(object_path) != item.sha256 or object_path.stat().st_size != item.bytes:
                    raise LakePublishError(f"local object does not match manifest: {item.key}")
                uploads.append(
                    (
                        item.key,
                        object_path,
                        "application/vnd.apache.parquet",
                        item.sha256,
                        item.bytes,
                    )
                )
        uploads.append(
            (
                manifest_key,
                manifest_path,
                "application/json",
                release_dataset.manifest_sha256,
                len(manifest_payload),
            )
        )
    uploads.append(
        (
            expected_release_key,
            resolved_release_path,
            "application/json",
            hashlib.sha256(release_payload).hexdigest(),
            len(release_payload),
        )
    )

    validate_lake_release_policy(
        release,
        manifests,
        evaluated_at=_utc_now(),
    )
    for source in referenced_sources.values():
        if isinstance(source, LakeRawIngestSourceRef):
            uploads.extend(
                (
                    (
                        source.key,
                        _mirror_path(mirror_root, source.key),
                        "application/octet-stream",
                        source.sha256,
                        _mirror_path(mirror_root, source.key).stat().st_size,
                    ),
                    (
                        source.metadata_key,
                        _mirror_path(mirror_root, source.metadata_key),
                        "application/json",
                        source.metadata_sha256,
                        _mirror_path(mirror_root, source.metadata_key).stat().st_size,
                    ),
                )
            )

    uploaded = 0
    reused = 0
    for key, path, content_type, expected_sha256, expected_size in uploads:
        if _ensure_immutable(
            store,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        ):
            uploaded += 1
        else:
            reused += 1

    inventory: dict[str, tuple[str, int, str]] = {}
    for key, _, content_type, expected_sha256, expected_size in uploads:
        expected = (expected_sha256, expected_size, content_type)
        previous_expected = inventory.setdefault(key, expected)
        if previous_expected != expected:
            raise LakePublishError(f"remote graph has conflicting identities: {key}")
    # This is the pointer precondition, not merely a per-upload postcondition.
    # Content bytes were bound to these identities by Content-MD5 on immutable PUT;
    # the closure pass proves every reachable node still has that identity now.
    for key, (expected_sha256, expected_size, content_type) in sorted(inventory.items()):
        _verify_remote_identity(
            store,
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        )

    pointer_key = lake_current_l1_pointer_key()
    current = store.head(pointer_key)
    previous_release_id = None
    previous_manifest_sha256 = None
    if current is not None:
        if current.size > _MAX_POINTER_BYTES:
            raise LakePublishError("L1 current pointer exceeds its size limit")
        current_payload = store.get_bytes(pointer_key)
        _verify_remote_small_object(
            store,
            key=pointer_key,
            expected_sha256=hashlib.sha256(current_payload).hexdigest(),
            expected_size=len(current_payload),
            content_type="application/json",
        )
        previous = load_lake_model_json(current_payload, L1ReleasePointer)
        if previous.release_id == release.release_id:
            if (
                previous.manifest_key != expected_release_key
                or previous.manifest_sha256 != hashlib.sha256(release_payload).hexdigest()
            ):
                raise LakePublishError("current pointer reuses release ID with different identity")
            return PublishReport(
                release_id=release.release_id,
                uploaded_objects=uploaded,
                reused_objects=reused,
                pointer_etag=current.etag,
            )
        previous_release_id = previous.release_id
        previous_manifest_sha256 = previous.manifest_sha256
    release_sha256 = hashlib.sha256(release_payload).hexdigest()
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=expected_release_key,
        manifest_sha256=release_sha256,
        previous_release_id=previous_release_id,
        previous_manifest_sha256=previous_manifest_sha256,
    )
    with tempfile.NamedTemporaryFile(prefix="baibai-l1-pointer-", suffix=".json") as temporary:
        temporary.write(canonical_json_bytes(pointer))
        temporary.flush()
        pointer_payload = Path(temporary.name).read_bytes()
        try:
            result = store.put_file(
                pointer_key,
                Path(temporary.name),
                sha256=hashlib.sha256(pointer_payload).hexdigest(),
                content_md5=_content_md5(Path(temporary.name)),
                content_type="application/json",
                if_match=current.etag if current is not None else None,
                if_none_match=current is None,
            )
        except LakeCASConflict:
            raise
        except Exception as exc:
            raise LakePublishError("L1 current pointer switch failed") from exc
    _verify_remote_small_object(
        store,
        key=pointer_key,
        expected_sha256=hashlib.sha256(pointer_payload).hexdigest(),
        expected_size=len(pointer_payload),
        content_type="application/json",
    )
    return PublishReport(
        release_id=release.release_id,
        uploaded_objects=uploaded,
        reused_objects=reused,
        pointer_etag=result.etag,
    )


def publish_calibration_bundle(
    *,
    mirror_root: Path,
    bundle_manifest_path: Path,
    store: ObjectStore,
) -> CalibrationBundlePublishReport:
    """Publish a complete three-dataset graph, then switch one bundle pointer by CAS."""

    root = mirror_root.resolve()
    resolved_bundle_path = bundle_manifest_path.resolve()
    if not resolved_bundle_path.is_relative_to(root):
        raise LakePublishError("calibration bundle manifest path escapes mirror root")
    bundle_payload = resolved_bundle_path.read_bytes()
    bundle = load_lake_model_json(bundle_payload, CalibrationBundleManifest)
    bundle_sha256 = hashlib.sha256(bundle_payload).hexdigest()
    bundle_reference = CalibrationBundleRef(
        bundle_id=bundle.bundle_id,
        manifest_key=f"lake/manifests/calibration-bundles/{bundle.bundle_id}.json",
        manifest_sha256=bundle_sha256,
    )
    local_pointer_path = _mirror_path(mirror_root, lake_current_calibration_bundle_pointer_key())
    local_pointer = load_lake_model_json(local_pointer_path.read_bytes(), CalibrationBundlePointer)
    if local_pointer.current != bundle_reference:
        raise LakePublishError("calibration bundle is not the local current pointer identity")
    if resolved_bundle_path != _mirror_path(
        mirror_root, f"lake/manifests/calibration-bundles/{bundle.bundle_id}.json"
    ):
        raise LakePublishError("calibration bundle path does not match its identity")

    uploads: dict[str, tuple[Path, str, str, int]] = {}
    dataset_manifests: dict[str, LakeDatasetManifest] = {}
    for name, reference in bundle.datasets.items():
        manifest_path = _mirror_path(mirror_root, reference.manifest_key)
        manifest_payload = manifest_path.read_bytes()
        if hashlib.sha256(manifest_payload).hexdigest() != reference.manifest_sha256:
            raise LakePublishError(f"calibration bundle dataset digest differs: {name}")
        manifest = load_lake_model_json(manifest_payload, LakeDatasetManifest)
        if (
            manifest.dataset != name
            or manifest.build_id != reference.build_id
            or manifest.totals.rows != reference.rows
        ):
            raise LakePublishError(f"calibration bundle dataset identity differs: {name}")
        dataset_manifests[name] = manifest
        for source in manifest.sources:
            source_path = resolve_lake_source_ref(mirror_root, source)
            uploads[source.key] = (
                source_path,
                (
                    "application/vnd.sqlite3"
                    if isinstance(source, LakeSQLiteSnapshotSourceRef)
                    else "application/json"
                ),
                source.sha256,
                source_path.stat().st_size,
            )
            if isinstance(source, CalibrationInputSourceRef):
                input_manifest = load_lake_model_json(
                    source_path.read_bytes(), CalibrationInputManifest
                )
                for item in input_manifest.files.values():
                    archived = _mirror_path(mirror_root, item.key)
                    uploads[item.key] = (
                        archived,
                        "application/octet-stream",
                        item.sha256,
                        item.bytes,
                    )
        for partition in manifest.partitions:
            for lake_object in partition.objects:
                object_path = _mirror_path(mirror_root, lake_object.key)
                if (
                    _sha256(object_path) != lake_object.sha256
                    or object_path.stat().st_size != lake_object.bytes
                ):
                    raise LakePublishError(
                        f"local object does not match manifest: {lake_object.key}"
                    )
                uploads[lake_object.key] = (
                    object_path,
                    "application/vnd.apache.parquet",
                    lake_object.sha256,
                    lake_object.bytes,
                )
        uploads[reference.manifest_key] = (
            manifest_path,
            "application/json",
            reference.manifest_sha256,
            len(manifest_payload),
        )
    for asof, cohort in bundle.cohorts.items():
        expected = {
            "calibration.panel": cohort.panel,
            "calibration.panel_diagnostics": cohort.diagnostics,
            "calibration.forward": cohort.forward,
        }
        for name, entry in expected.items():
            if dataset_manifests[name].cohort_inventory.get(asof) != entry:
                raise LakePublishError(f"calibration bundle cohort inventory differs: {asof}")
    if any(
        set(manifest.cohort_inventory) != set(bundle.cohorts)
        for manifest in dataset_manifests.values()
    ):
        raise LakePublishError("calibration bundle omits a dataset cohort inventory entry")
    if {manifest.producer_git_commit for manifest in dataset_manifests.values()} != {
        bundle.producer_git_commit
    }:
        raise LakePublishError("calibration bundle producer identity differs")
    source_sets = {
        tuple((item.kind, item.source_id, item.key, item.sha256) for item in manifest.sources)
        for manifest in dataset_manifests.values()
    }
    if len(source_sets) != 1:
        raise LakePublishError("calibration bundle input generation differs")
    bundle_key = f"lake/manifests/calibration-bundles/{bundle.bundle_id}.json"
    uploads[bundle_key] = (
        resolved_bundle_path,
        "application/json",
        bundle_sha256,
        len(bundle_payload),
    )

    uploaded = 0
    reused = 0
    for key, (path, content_type, digest, size) in sorted(uploads.items()):
        if _ensure_immutable(
            store,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=digest,
            expected_size=size,
        ):
            uploaded += 1
        else:
            reused += 1

    pointer_key = lake_current_calibration_bundle_pointer_key()
    current = store.head(pointer_key)
    previous = None
    if current is not None:
        if current.size > _MAX_POINTER_BYTES:
            raise LakePublishError("calibration current pointer exceeds its size limit")
        current_payload = store.get_bytes(pointer_key)
        _verify_remote_small_object(
            store,
            key=pointer_key,
            expected_sha256=hashlib.sha256(current_payload).hexdigest(),
            expected_size=len(current_payload),
            content_type="application/json",
        )
        old_pointer = load_lake_model_json(current_payload, CalibrationBundlePointer)
        if old_pointer.current.bundle_id == bundle.bundle_id:
            if (
                old_pointer.current != bundle_reference
                or old_pointer.previous != local_pointer.previous
            ):
                raise LakePublishError(
                    "bundle ID is already current with different rollback identity"
                )
            _require_remote_calibration_closure(store, old_pointer.current)
            if old_pointer.previous is not None:
                _require_remote_calibration_closure(store, old_pointer.previous)
            return CalibrationBundlePublishReport(
                bundle_id=bundle.bundle_id,
                uploaded_objects=uploaded,
                reused_objects=reused,
                pointer_etag=current.etag,
            )
        previous = old_pointer.current
        if local_pointer.previous != previous:
            raise LakePublishError("remote current is not the local rollback identity")
        _require_remote_calibration_closure(store, previous)
    remote_pointer = CalibrationBundlePointer(
        current=bundle_reference,
        previous=previous,
    )
    pointer_payload = canonical_manifest_bytes(remote_pointer)
    with tempfile.NamedTemporaryFile(
        prefix="baibai-calibration-pointer-", suffix=".json"
    ) as temporary:
        temporary.write(pointer_payload)
        temporary.flush()
        try:
            result = store.put_file(
                pointer_key,
                Path(temporary.name),
                sha256=hashlib.sha256(pointer_payload).hexdigest(),
                content_md5=_content_md5(Path(temporary.name)),
                content_type="application/json",
                if_match=current.etag if current is not None else None,
                if_none_match=current is None,
            )
        except LakeCASConflict:
            raise
        except Exception as exc:
            raise LakePublishError("calibration bundle pointer switch failed") from exc
    _verify_remote_small_object(
        store,
        key=pointer_key,
        expected_sha256=hashlib.sha256(pointer_payload).hexdigest(),
        expected_size=len(pointer_payload),
        content_type="application/json",
    )
    return CalibrationBundlePublishReport(
        bundle_id=bundle.bundle_id,
        uploaded_objects=uploaded,
        reused_objects=reused,
        pointer_etag=result.etag,
    )


def rollback_calibration_bundle(*, store: ObjectStore) -> CalibrationBundlePublishReport:
    """Atomically exchange calibration current and previous after proving closure."""

    pointer_key = lake_current_calibration_bundle_pointer_key()
    remote = store.head(pointer_key)
    if remote is None:
        raise LakePublishError("calibration current pointer is absent")
    if remote.size > _MAX_POINTER_BYTES:
        raise LakePublishError("calibration current pointer exceeds its size limit")
    payload = store.get_bytes(pointer_key)
    _verify_remote_small_object(
        store,
        key=pointer_key,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
        content_type="application/json",
    )
    pointer = load_lake_model_json(payload, CalibrationBundlePointer)
    if pointer.previous is None:
        raise LakePublishError("calibration current pointer has no rollback generation")
    _require_remote_calibration_closure(store, pointer.previous)
    rolled_back = CalibrationBundlePointer(current=pointer.previous, previous=pointer.current)
    rollback_payload = canonical_manifest_bytes(rolled_back)
    with tempfile.NamedTemporaryFile(
        prefix="baibai-calibration-rollback-", suffix=".json"
    ) as temporary:
        temporary.write(rollback_payload)
        temporary.flush()
        try:
            result = store.put_file(
                pointer_key,
                Path(temporary.name),
                sha256=hashlib.sha256(rollback_payload).hexdigest(),
                content_md5=_content_md5(Path(temporary.name)),
                content_type="application/json",
                if_match=remote.etag,
            )
        except LakeCASConflict:
            raise
        except Exception as exc:
            raise LakePublishError("calibration bundle rollback failed") from exc
    _verify_remote_small_object(
        store,
        key=pointer_key,
        expected_sha256=hashlib.sha256(rollback_payload).hexdigest(),
        expected_size=len(rollback_payload),
        content_type="application/json",
    )
    return CalibrationBundlePublishReport(
        bundle_id=rolled_back.current.bundle_id,
        uploaded_objects=0,
        reused_objects=0,
        pointer_etag=result.etag,
    )


def _require_remote_calibration_closure(
    store: ObjectStore, reference: CalibrationBundleRef
) -> None:
    bundle = _remote_json_model(
        store,
        key=reference.manifest_key,
        expected_sha256=reference.manifest_sha256,
        model=CalibrationBundleManifest,
    )
    if bundle.bundle_id != reference.bundle_id:
        raise LakePublishError("remote calibration bundle identity differs")
    manifests: dict[str, LakeDatasetManifest] = {}
    for name, dataset_ref in bundle.datasets.items():
        manifest = _remote_json_model(
            store,
            key=dataset_ref.manifest_key,
            expected_sha256=dataset_ref.manifest_sha256,
            model=LakeDatasetManifest,
        )
        if (
            manifest.dataset != name
            or manifest.build_id != dataset_ref.build_id
            or manifest.totals.rows != dataset_ref.rows
        ):
            raise LakePublishError(f"remote calibration dataset identity differs: {name}")
        manifests[name] = manifest
        for source in manifest.sources:
            if isinstance(source, CalibrationInputSourceRef):
                input_manifest = _remote_json_model(
                    store,
                    key=source.key,
                    expected_sha256=source.sha256,
                    model=CalibrationInputManifest,
                )
                if input_manifest.input_id != source.source_id:
                    raise LakePublishError("remote calibration input identity differs")
                for archived_item in input_manifest.files.values():
                    _verify_remote_identity(
                        store,
                        key=archived_item.key,
                        expected_sha256=archived_item.sha256,
                        expected_size=archived_item.bytes,
                        content_type="application/octet-stream",
                    )
            elif isinstance(source, LakeSQLiteSnapshotSourceRef):
                remote_source = store.head(source.key)
                if remote_source is None:
                    raise LakePublishError(f"remote object is missing: {source.key}")
                _verify_remote_identity(
                    store,
                    key=source.key,
                    expected_sha256=source.sha256,
                    expected_size=remote_source.size,
                    content_type="application/vnd.sqlite3",
                )
            elif isinstance(source, LakeRawIngestSourceRef):
                remote_source = store.head(source.key)
                if remote_source is None:
                    raise LakePublishError(f"remote object is missing: {source.key}")
                _verify_remote_identity(
                    store,
                    key=source.key,
                    expected_sha256=source.sha256,
                    expected_size=remote_source.size,
                    content_type="application/octet-stream",
                )
                metadata = store.head(source.metadata_key)
                if metadata is None:
                    raise LakePublishError(f"remote object is missing: {source.metadata_key}")
                _verify_remote_small_object(
                    store,
                    key=source.metadata_key,
                    expected_sha256=source.metadata_sha256,
                    expected_size=metadata.size,
                    content_type="application/json",
                )
            else:
                remote_source = store.head(source.key)
                if remote_source is None:
                    raise LakePublishError(f"remote object is missing: {source.key}")
                _verify_remote_small_object(
                    store,
                    key=source.key,
                    expected_sha256=source.sha256,
                    expected_size=remote_source.size,
                    content_type="application/json",
                )
        for partition in manifest.partitions:
            for lake_object in partition.objects:
                _verify_remote_identity(
                    store,
                    key=lake_object.key,
                    expected_sha256=lake_object.sha256,
                    expected_size=lake_object.bytes,
                    content_type="application/vnd.apache.parquet",
                )
    for asof, cohort in bundle.cohorts.items():
        expected = {
            "calibration.panel": cohort.panel,
            "calibration.panel_diagnostics": cohort.diagnostics,
            "calibration.forward": cohort.forward,
        }
        if any(
            manifests[name].cohort_inventory.get(asof) != entry for name, entry in expected.items()
        ):
            raise LakePublishError(f"remote calibration cohort inventory differs: {asof}")


def _remote_json_model[ModelT: BaseModel](
    store: ObjectStore,
    *,
    key: str,
    expected_sha256: str,
    model: type[ModelT],
) -> ModelT:
    remote = store.head(key)
    if remote is None:
        raise LakePublishError(f"remote object is missing: {key}")
    _verify_remote_small_object(
        store,
        key=key,
        expected_sha256=expected_sha256,
        expected_size=remote.size,
        content_type="application/json",
    )
    return load_lake_model_json(store.get_bytes(key), model)


def _ensure_immutable(
    store: ObjectStore,
    *,
    key: str,
    path: Path,
    content_type: str,
    expected_sha256: str,
    expected_size: int,
) -> bool:
    existing = store.head(key)
    if existing is not None:
        _verify_remote_identity(
            store,
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        )
        return False
    with _sealed_upload(path) as (sealed, digest, content_md5, size):
        if digest != expected_sha256 or size != expected_size:
            raise LakePublishError(f"local upload source differs from graph identity: {key}")
        try:
            store.put_file(
                key,
                sealed,
                sha256=digest,
                content_md5=content_md5,
                content_type=content_type,
                if_none_match=True,
            )
        except LakeCASConflict:
            _verify_remote_identity(
                store,
                key=key,
                expected_sha256=digest,
                expected_size=size,
                content_type=content_type,
            )
            return False
        _verify_remote_identity(
            store,
            key=key,
            expected_sha256=digest,
            expected_size=size,
            content_type=content_type,
        )
        return True


class AwsCliR2Store:
    """Small S3-compatible R2 adapter using the repository's existing AWS CLI dependency."""

    def __init__(self, *, bucket: str, env: Mapping[str, str] | None = None) -> None:
        source = dict(os.environ if env is None else env)
        account_id = _required(source, "R2_ACCOUNT_ID")
        if _R2_ACCOUNT_ID.fullmatch(account_id) is None:
            raise LakePublishError("R2_ACCOUNT_ID must be 32 lowercase hexadecimal characters")
        self.bucket = bucket
        self.endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self.env = {
            key: source[key]
            for key in ("HOME", "PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if key in source
        } | {
            "AWS_ACCESS_KEY_ID": _required(source, "R2_ACCESS_KEY_ID"),
            "AWS_SECRET_ACCESS_KEY": _required(source, "R2_SECRET_ACCESS_KEY"),
            "AWS_DEFAULT_REGION": "auto",
        }

    def head(self, key: str) -> RemoteObject | None:
        result = self._run("head-object", "--key", key, allow_missing=True)
        if result is None:
            return None
        value = json.loads(result.stdout)
        return RemoteObject(
            etag=str(value["ETag"]).strip('"'),
            size=int(value["ContentLength"]),
            metadata={str(k): str(v) for k, v in value.get("Metadata", {}).items()},
            content_type=str(value.get("ContentType", "application/octet-stream")),
        )

    def get_bytes(self, key: str) -> bytes:
        with tempfile.NamedTemporaryFile() as target:
            result = self._run("get-object", "--key", key, target.name)
            assert result is not None
            return Path(target.name).read_bytes()

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
        arguments = [
            "put-object",
            "--key",
            key,
            "--body",
            str(path),
            "--content-type",
            content_type,
            "--metadata",
            json.dumps(
                {
                    "sha256": sha256,
                    "content-md5": content_md5,
                    "integrity": "content-md5-v1",
                },
                separators=(",", ":"),
            ),
            "--content-md5",
            content_md5,
        ]
        if if_match is not None:
            arguments.extend(("--if-match", if_match))
        if if_none_match:
            arguments.extend(("--if-none-match", "*"))
        try:
            self._run(*arguments)
        except LakeCASConflict:
            raise
        result = self.head(key)
        if result is None:
            raise LakePublishError(f"R2 object missing after successful put: {key}")
        return result

    def _run(
        self,
        operation: str,
        *arguments: str,
        allow_missing: bool = False,
    ) -> subprocess.CompletedProcess[str] | None:
        command = (
            "aws",
            "s3api",
            operation,
            "--bucket",
            self.bucket,
            *arguments,
            "--endpoint-url",
            self.endpoint,
            "--output",
            "json",
            "--no-cli-pager",
        )
        try:
            result = subprocess.run(  # nosec B603
                command,
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise LakePublishError(f"R2 {operation} timed out") from exc
        if result.returncode == 0:
            return result
        error = result.stderr
        if allow_missing and ("Not Found" in error or "404" in error or "NoSuchKey" in error):
            return None
        if (
            "PreconditionFailed" in error
            or "ConditionalRequestConflict" in error
            or "412" in error
            or "409" in error
        ):
            raise LakeCASConflict(f"R2 conditional write conflict: {operation}")
        raise LakePublishError(f"R2 {operation} failed with exit code {result.returncode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--release-manifest", type=Path)
    target.add_argument("--raw-metadata", type=Path)
    target.add_argument("--calibration-bundle", type=Path)
    target.add_argument("--rollback-calibration", action="store_true")
    parser.add_argument("--bucket", default="baibai-stores")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = AwsCliR2Store(bucket=args.bucket)
    if args.rollback_calibration:
        rollback_report = rollback_calibration_bundle(store=store)
        print(json.dumps(rollback_report.__dict__, sort_keys=True))
        return 0
    if args.mirror is None:
        parser.error("--mirror is required for publication")
    if args.raw_metadata is not None:
        raw_report = publish_raw_archive(
            mirror_root=args.mirror,
            metadata_path=args.raw_metadata,
            store=store,
        )
        print(json.dumps(raw_report.__dict__, sort_keys=True))
        return 0
    if args.calibration_bundle is not None:
        bundle_report = publish_calibration_bundle(
            mirror_root=args.mirror,
            bundle_manifest_path=args.calibration_bundle,
            store=store,
        )
        print(json.dumps(bundle_report.__dict__, sort_keys=True))
        return 0
    assert args.release_manifest is not None
    release_report = publish_l1_release(
        mirror_root=args.mirror,
        release_manifest_path=args.release_manifest,
        store=store,
    )
    print(json.dumps(release_report.__dict__, sort_keys=True))
    return 0


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise LakePublishError(f"required environment variable is missing: {name}")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _mirror_path(mirror_root: Path, key: str) -> Path:
    root = mirror_root.resolve()
    path = (root / key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LakePublishError(f"lake object escapes mirror root: {key}") from exc
    return path


@contextmanager
def _sealed_upload(path: Path) -> Iterator[tuple[Path, str, str, int]]:
    """Capture one stable local byte sequence for hash, upload, and verification."""
    digest = hashlib.sha256()
    transport_digest = hashlib.md5(usedforsecurity=False)  # nosec B324
    size = 0
    temporary_path: Path | None = None
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            with tempfile.NamedTemporaryFile(prefix="baibai-lake-upload-", delete=False) as target:
                temporary_path = Path(target.name)
                while chunk := source.read(8 * 1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
                    transport_digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or size != after.st_size:
            raise LakePublishError(f"local upload source changed while sealing: {path}")
        assert temporary_path is not None
        yield (
            temporary_path,
            digest.hexdigest(),
            base64.b64encode(transport_digest.digest()).decode(),
            size,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _verify_remote_identity(
    store: ObjectStore,
    *,
    key: str,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> None:
    remote = store.head(key)
    if (
        remote is None
        or remote.size != expected_size
        or remote.metadata.get("sha256") != expected_sha256
        or remote.metadata.get("integrity") != "content-md5-v1"
        or not remote.metadata.get("content-md5")
        or remote.content_type != content_type
    ):
        raise LakePublishError(f"remote object metadata postcondition failed: {key}")


def _verify_remote_small_object(
    store: ObjectStore,
    *,
    key: str,
    expected_sha256: str,
    expected_size: int,
    content_type: str,
) -> None:
    _verify_remote_identity(
        store,
        key=key,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        content_type=content_type,
    )
    payload = store.get_bytes(key)
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise LakePublishError(f"remote object bytes postcondition failed: {key}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_md5(path: Path) -> str:
    """Return base64 Content-MD5 for R2 transport validation, not logical identity."""
    digest = hashlib.md5(usedforsecurity=False)  # nosec B324 - transport checksum only.
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


if __name__ == "__main__":
    raise SystemExit(main())
