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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from baibai_engine.batch_api import (
    CalibrationBundleManifest,
    CalibrationBundlePointer,
    CalibrationBundleRef,
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
    lake_verified_source_scope,
    load_lake_model_json,
    resolve_lake_source_ref,
    validate_lake_release_policy,
)

_R2_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_POINTER_BYTES = 64 * 1024
_MAX_SMALL_OBJECT_BYTES = 16 * 1024 * 1024
# A subprocess deadline that is shorter than the transfer it guards turns a slow
# object into an ambiguous outcome. The floor covers control-plane latency; the
# transfer term is derived from the object's own size at a throughput well below
# the 2 GB / 181 s upload this store has actually measured.
_BASE_OPERATION_TIMEOUT_SECONDS = 120
_MIN_TRANSFER_BYTES_PER_SECOND = 4 * 1024 * 1024


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

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None: ...

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
class TransferReport:
    """What this publication actually moved, so "differential" is an observation.

    Object counts alone cannot tell a run that uploaded one changed month from a run
    that re-read the whole history to check it, which is exactly the difference this
    architecture exists to produce.
    """

    uploaded_objects: int
    reused_objects: int
    uploaded_bytes: int
    downloaded_bytes: int
    head_requests: int
    get_requests: int

    def as_dict(self) -> dict[str, int]:
        return {
            "downloaded_bytes": self.downloaded_bytes,
            "get_requests": self.get_requests,
            "head_requests": self.head_requests,
            "reused_objects": self.reused_objects,
            "uploaded_bytes": self.uploaded_bytes,
            "uploaded_objects": self.uploaded_objects,
        }


@dataclass(frozen=True)
class PublishReport:
    release_id: str
    transfers: TransferReport
    pointer_etag: str

    def as_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "pointer_etag": self.pointer_etag,
            **self.transfers.as_dict(),
        }


@dataclass(frozen=True)
class CalibrationBundlePublishReport:
    bundle_id: str
    transfers: TransferReport
    pointer_etag: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "pointer_etag": self.pointer_etag,
            **self.transfers.as_dict(),
        }


@dataclass(frozen=True)
class RawPublishReport:
    ingest_id: str
    transfers: TransferReport

    def as_dict(self) -> dict[str, object]:
        return {"ingest_id": self.ingest_id, **self.transfers.as_dict()}


@dataclass
class _RemotePublication:
    """One publication's remote reads: counted, bounded, and never repeated.

    An immutable key is proved once per publication. Verifying it again for the
    pointer precondition would double the bytes this run moves without adding a
    guarantee, because nothing in between can replace a key the store refuses to
    overwrite.

    A newly written object is read back in full: that is the only evidence that the
    bytes this run sealed are the bytes the store now holds. An object that was
    already present is proved by its identity metadata, which was bound to its bytes
    by Content-MD5 on the immutable write, and by the reader, which fails closed on
    the SHA-256 of everything it uses. ``verify_bytes`` turns the full stream back on
    for an audit that deliberately pays for it.
    """

    store: ObjectStore
    verify_bytes: bool = False
    verified: dict[str, tuple[str, int, str]] = field(default_factory=dict)
    manifest_payloads: dict[str, bytes] = field(default_factory=dict)
    uploaded_objects: int = 0
    reused_objects: int = 0
    uploaded_bytes: int = 0
    downloaded_bytes: int = 0
    head_requests: int = 0
    get_requests: int = 0

    def report(self) -> TransferReport:
        return TransferReport(
            uploaded_objects=self.uploaded_objects,
            reused_objects=self.reused_objects,
            uploaded_bytes=self.uploaded_bytes,
            downloaded_bytes=self.downloaded_bytes,
            head_requests=self.head_requests,
            get_requests=self.get_requests,
        )

    def head(self, key: str) -> RemoteObject | None:
        self.head_requests += 1
        return self.store.head(key)

    def get_bytes(self, key: str) -> bytes:
        payload = self.store.get_bytes(key)
        self.get_requests += 1
        self.downloaded_bytes += len(payload)
        return payload

    def require_identity(
        self,
        *,
        key: str,
        expected_sha256: str,
        expected_size: int,
        content_type: str,
        read_back: bool = False,
    ) -> None:
        """Prove one immutable key holds this identity, at most once per publication."""

        if self._already_verified(
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        ):
            return
        self.accept_existing(
            self.head(key),
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
            read_back=read_back,
        )

    def accept_existing(
        self,
        remote: RemoteObject | None,
        *,
        key: str,
        expected_sha256: str,
        expected_size: int,
        content_type: str,
        read_back: bool = False,
    ) -> None:
        """Prove an already-fetched HEAD result describes the identity the graph names."""

        expected = (expected_sha256, expected_size, content_type)
        if self._already_verified(
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        ):
            return
        if remote is None:
            raise LakePublishError(f"remote object is missing: {key}")
        if (
            remote.size != expected_size
            or remote.metadata.get("sha256") != expected_sha256
            or remote.metadata.get("integrity") != "content-md5-v1"
            or not remote.metadata.get("content-md5")
            or remote.content_type != content_type
        ):
            raise LakePublishError(f"remote object metadata postcondition failed: {key}")
        if read_back or self.verify_bytes:
            self._require_bytes(
                key=key, expected_sha256=expected_sha256, expected_size=expected_size
            )
        self.verified[key] = expected

    def _already_verified(
        self, *, key: str, expected_sha256: str, expected_size: int, content_type: str
    ) -> bool:
        previous = self.verified.get(key)
        if previous is None:
            return False
        if previous != (expected_sha256, expected_size, content_type):
            raise LakePublishError(f"remote graph has conflicting identities: {key}")
        return True

    def put(
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
        """Write one object and record the request the adapter makes to prove it landed.

        Every remote request this publication causes is counted in one place. Letting the
        adapter make an uncounted one leaves the transfer report arguing for differential
        cost with a figure smaller than the cost.
        """

        result = self.store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=content_md5,
            content_type=content_type,
            if_match=if_match,
            if_none_match=if_none_match,
        )
        self.head_requests += 1
        return result

    def require_small_object(self, *, key: str, expected_sha256: str, content_type: str) -> bytes:
        """Read one manifest-sized object once, proving its identity from its bytes.

        Manifests and pointers are read for their content anyway, so their identity is
        proved from the bytes that were read rather than from a second stream.
        """

        cached = self.manifest_payloads.get(key)
        if cached is not None:
            if hashlib.sha256(cached).hexdigest() != expected_sha256:
                raise LakePublishError(f"remote graph has conflicting identities: {key}")
            return cached
        remote = self.head(key)
        if remote is None:
            raise LakePublishError(f"remote object is missing: {key}")
        if remote.size > _MAX_SMALL_OBJECT_BYTES:
            raise LakePublishError(f"remote graph node exceeds its size limit: {key}")
        payload = self.get_bytes(key)
        if len(payload) != remote.size or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise LakePublishError(f"remote object bytes postcondition failed: {key}")
        self.accept_existing(
            remote,
            key=key,
            expected_sha256=expected_sha256,
            expected_size=len(payload),
            content_type=content_type,
        )
        self.manifest_payloads[key] = payload
        return payload

    def read_pointer(self, key: str, remote: RemoteObject) -> bytes:
        """Read the one mutable object, proving its bytes against its own identity."""

        if remote.size > _MAX_POINTER_BYTES:
            raise LakePublishError(f"remote pointer exceeds its size limit: {key}")
        payload = self.get_bytes(key)
        if (
            len(payload) != remote.size
            or remote.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
            or remote.metadata.get("integrity") != "content-md5-v1"
            or not remote.metadata.get("content-md5")
            or remote.content_type != "application/json"
        ):
            raise LakePublishError(f"remote pointer bytes differ from their identity: {key}")
        return payload

    def require_pointer_bytes(self, *, key: str, expected: bytes) -> None:
        """Prove the pointer this run switched now holds exactly the bytes it wrote."""

        remote = self.head(key)
        if remote is None:
            raise LakePublishError(f"remote pointer is missing after its switch: {key}")
        if self.read_pointer(key, remote) != expected:
            raise LakePublishError(f"remote pointer bytes postcondition failed: {key}")

    def require_present_identity(
        self, *, key: str, expected_sha256: str, content_type: str
    ) -> None:
        """Prove one object whose size the manifest does not fix still holds its digest."""

        stored = self.verified.get(key)
        if stored is not None:
            if (stored[0], stored[2]) != (expected_sha256, content_type):
                raise LakePublishError(f"remote graph has conflicting identities: {key}")
            return
        remote = self.head(key)
        if remote is None:
            raise LakePublishError(f"remote object is missing: {key}")
        self.accept_existing(
            remote,
            key=key,
            expected_sha256=expected_sha256,
            expected_size=remote.size,
            content_type=content_type,
        )

    def _require_bytes(self, *, key: str, expected_sha256: str, expected_size: int) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="baibai-lake-readback-", delete=False
            ) as target:
                temporary_path = Path(target.name)
            self.store.download_file(key, temporary_path, expect_bytes=expected_size)
            self.get_requests += 1
            self.downloaded_bytes += temporary_path.stat().st_size
            if (
                temporary_path.stat().st_size != expected_size
                or _sha256(temporary_path) != expected_sha256
            ):
                raise LakePublishError(f"remote object bytes postcondition failed: {key}")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def publish_raw_archive(
    *,
    mirror_root: Path,
    metadata_path: Path,
    store: ObjectStore,
    verify_bytes: bool = False,
) -> RawPublishReport:
    publication = _RemotePublication(store=store, verify_bytes=verify_bytes)
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
    for key, path, content_type, expected_sha256, expected_size in uploads:
        _ensure_immutable(
            publication,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
    return RawPublishReport(ingest_id=metadata.ingest_id, transfers=publication.report())


def publish_l1_release(
    *,
    mirror_root: Path,
    release_manifest_path: Path,
    store: ObjectStore,
    verify_bytes: bool = False,
) -> PublishReport:
    """Upload immutable graph nodes, then atomically switch the one mutable pointer."""
    publication = _RemotePublication(store=store, verify_bytes=verify_bytes)
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
                if isinstance(source, LakeRawIngestSourceRef):
                    resolve_lake_source_ref(mirror_root, source)
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

    # Every reachable node is proved once. The publication memo makes this both the
    # per-upload postcondition and the pointer precondition: nothing between the two
    # can replace a key the store refuses to overwrite, so re-reading the closure
    # would move the whole history's bytes again to learn what is already known.
    inventory: dict[str, tuple[str, int, str]] = {}
    for key, _, content_type, expected_sha256, expected_size in uploads:
        expected = (expected_sha256, expected_size, content_type)
        previous_expected = inventory.setdefault(key, expected)
        if previous_expected != expected:
            raise LakePublishError(f"remote graph has conflicting identities: {key}")
    for key, path, content_type, expected_sha256, expected_size in uploads:
        _ensure_immutable(
            publication,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )

    pointer_key = lake_current_l1_pointer_key()
    current = publication.head(pointer_key)
    previous_release_id = None
    previous_manifest_sha256 = None
    if current is not None:
        previous = load_lake_model_json(
            publication.read_pointer(pointer_key, current), L1ReleasePointer
        )
        if previous.release_id == release.release_id:
            if (
                previous.manifest_key != expected_release_key
                or previous.manifest_sha256 != hashlib.sha256(release_payload).hexdigest()
            ):
                raise LakePublishError("current pointer reuses release ID with different identity")
            return PublishReport(
                release_id=release.release_id,
                transfers=publication.report(),
                pointer_etag=current.etag,
            )
        # A pointer that names a rollback generation is a promise that the generation
        # can be restored. Publishing over an unverifiable current would turn that
        # promise into a claim nobody checked until the day it is needed.
        _require_remote_l1_closure(
            publication,
            release_id=previous.release_id,
            manifest_key=previous.manifest_key,
            manifest_sha256=previous.manifest_sha256,
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
            result = publication.put(
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
    publication.require_pointer_bytes(key=pointer_key, expected=pointer_payload)
    return PublishReport(
        release_id=release.release_id,
        transfers=publication.report(),
        pointer_etag=result.etag,
    )


def rollback_l1_release(*, store: ObjectStore) -> PublishReport:
    """Atomically exchange L1 current and previous after proving the target's closure."""

    publication = _RemotePublication(store=store)
    pointer_key = lake_current_l1_pointer_key()
    remote = publication.head(pointer_key)
    if remote is None:
        raise LakePublishError("L1 current pointer is absent")
    pointer = load_lake_model_json(publication.read_pointer(pointer_key, remote), L1ReleasePointer)
    if pointer.previous_release_id is None or pointer.previous_manifest_sha256 is None:
        raise LakePublishError("L1 current pointer has no rollback generation")
    target_key = lake_release_manifest_key(release_id=pointer.previous_release_id)
    _require_remote_l1_closure(
        publication,
        release_id=pointer.previous_release_id,
        manifest_key=target_key,
        manifest_sha256=pointer.previous_manifest_sha256,
    )
    rolled_back = L1ReleasePointer(
        release_id=pointer.previous_release_id,
        manifest_key=target_key,
        manifest_sha256=pointer.previous_manifest_sha256,
        previous_release_id=pointer.release_id,
        previous_manifest_sha256=pointer.manifest_sha256,
    )
    rollback_payload = canonical_json_bytes(rolled_back)
    with tempfile.NamedTemporaryFile(prefix="baibai-l1-rollback-", suffix=".json") as temporary:
        temporary.write(rollback_payload)
        temporary.flush()
        try:
            result = publication.put(
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
            raise LakePublishError("L1 current pointer rollback failed") from exc
    publication.require_pointer_bytes(key=pointer_key, expected=rollback_payload)
    return PublishReport(
        release_id=rolled_back.release_id,
        transfers=publication.report(),
        pointer_etag=result.etag,
    )


def _require_remote_l1_closure(
    publication: _RemotePublication,
    *,
    release_id: str,
    manifest_key: str,
    manifest_sha256: str,
) -> None:
    """Prove one L1 release still resolves to a complete, digest-valid object graph."""

    if manifest_key != lake_release_manifest_key(release_id=release_id):
        raise LakePublishError("remote L1 release key does not match its identity")
    release = _remote_json_model(
        publication,
        key=manifest_key,
        expected_sha256=manifest_sha256,
        model=LakeReleaseManifest,
    )
    if release.release_id != release_id:
        raise LakePublishError("remote L1 release identity differs")
    for dataset_name, release_dataset in release.datasets.items():
        manifest = _remote_json_model(
            publication,
            key=lake_dataset_manifest_key(dataset=dataset_name, build_id=release_dataset.build_id),
            expected_sha256=release_dataset.manifest_sha256,
            model=LakeDatasetManifest,
        )
        if (
            manifest.dataset != dataset_name
            or manifest.build_id != release_dataset.build_id
            or manifest.contract_version != release_dataset.contract_version
            or manifest.totals != release_dataset.totals
        ):
            raise LakePublishError(f"remote L1 dataset identity differs: {dataset_name}")
        for partition in manifest.partitions:
            for item in partition.objects:
                publication.require_identity(
                    key=item.key,
                    expected_sha256=item.sha256,
                    expected_size=item.bytes,
                    content_type="application/vnd.apache.parquet",
                )
            for source in partition.sources:
                if isinstance(source, LakeRawIngestSourceRef):
                    publication.require_present_identity(
                        key=source.key,
                        expected_sha256=source.sha256,
                        content_type="application/octet-stream",
                    )
                    publication.require_small_object(
                        key=source.metadata_key,
                        expected_sha256=source.metadata_sha256,
                        content_type="application/json",
                    )


def publish_calibration_bundle(
    *,
    mirror_root: Path,
    bundle_manifest_path: Path,
    store: ObjectStore,
    verify_bytes: bool = False,
) -> CalibrationBundlePublishReport:
    """Publish a complete three-dataset graph, then switch one bundle pointer by CAS."""

    with lake_verified_source_scope():
        return _publish_calibration_bundle(
            mirror_root=mirror_root,
            bundle_manifest_path=bundle_manifest_path,
            store=store,
            verify_bytes=verify_bytes,
        )


def _publish_calibration_bundle(
    *,
    mirror_root: Path,
    bundle_manifest_path: Path,
    store: ObjectStore,
    verify_bytes: bool,
) -> CalibrationBundlePublishReport:
    publication = _RemotePublication(store=store, verify_bytes=verify_bytes)
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
    bundle_key = f"lake/manifests/calibration-bundles/{bundle.bundle_id}.json"
    uploads[bundle_key] = (
        resolved_bundle_path,
        "application/json",
        bundle_sha256,
        len(bundle_payload),
    )

    for key, (path, content_type, digest, size) in sorted(uploads.items()):
        _ensure_immutable(
            publication,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=digest,
            expected_size=size,
        )

    pointer_key = lake_current_calibration_bundle_pointer_key()
    current = publication.head(pointer_key)
    previous = None
    if current is not None:
        old_pointer = load_lake_model_json(
            publication.read_pointer(pointer_key, current), CalibrationBundlePointer
        )
        if old_pointer.current.bundle_id == bundle.bundle_id:
            if old_pointer.current != bundle_reference:
                raise LakePublishError(
                    "bundle ID is already current with different rollback identity"
                )
            _require_remote_calibration_closure(publication, old_pointer.current)
            if old_pointer.previous is not None:
                _require_remote_calibration_closure(publication, old_pointer.previous)
            return CalibrationBundlePublishReport(
                bundle_id=bundle.bundle_id,
                transfers=publication.report(),
                pointer_etag=current.etag,
            )
        # The rollback identity is the generation this publication is actually replacing,
        # which is whatever remote currently serves — not whatever the local store
        # happens to name as its own previous. Requiring those to agree would make the
        # remote reachable only from the local generation immediately after it: one
        # failed publication, and every later local generation is refused while the one
        # remote wants can no longer be produced, with no way back except editing a
        # pointer by hand.
        previous = old_pointer.current
        _require_remote_calibration_closure(publication, previous)
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
            result = publication.put(
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
    publication.require_pointer_bytes(key=pointer_key, expected=pointer_payload)
    return CalibrationBundlePublishReport(
        bundle_id=bundle.bundle_id,
        transfers=publication.report(),
        pointer_etag=result.etag,
    )


def rollback_calibration_bundle(*, store: ObjectStore) -> CalibrationBundlePublishReport:
    """Atomically exchange calibration current and previous after proving closure."""

    publication = _RemotePublication(store=store)
    pointer_key = lake_current_calibration_bundle_pointer_key()
    remote = publication.head(pointer_key)
    if remote is None:
        raise LakePublishError("calibration current pointer is absent")
    pointer = load_lake_model_json(
        publication.read_pointer(pointer_key, remote), CalibrationBundlePointer
    )
    if pointer.previous is None:
        raise LakePublishError("calibration current pointer has no rollback generation")
    _require_remote_calibration_closure(publication, pointer.previous)
    rolled_back = CalibrationBundlePointer(current=pointer.previous, previous=pointer.current)
    rollback_payload = canonical_manifest_bytes(rolled_back)
    with tempfile.NamedTemporaryFile(
        prefix="baibai-calibration-rollback-", suffix=".json"
    ) as temporary:
        temporary.write(rollback_payload)
        temporary.flush()
        try:
            result = publication.put(
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
    publication.require_pointer_bytes(key=pointer_key, expected=rollback_payload)
    return CalibrationBundlePublishReport(
        bundle_id=rolled_back.current.bundle_id,
        transfers=publication.report(),
        pointer_etag=result.etag,
    )


def _require_remote_calibration_closure(
    publication: _RemotePublication, reference: CalibrationBundleRef
) -> None:
    bundle = _remote_json_model(
        publication,
        key=reference.manifest_key,
        expected_sha256=reference.manifest_sha256,
        model=CalibrationBundleManifest,
    )
    if bundle.bundle_id != reference.bundle_id:
        raise LakePublishError("remote calibration bundle identity differs")
    manifests: dict[str, LakeDatasetManifest] = {}
    for name, dataset_ref in bundle.datasets.items():
        manifest = _remote_json_model(
            publication,
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
        for partition in manifest.partitions:
            for lake_object in partition.objects:
                publication.require_identity(
                    key=lake_object.key,
                    expected_sha256=lake_object.sha256,
                    expected_size=lake_object.bytes,
                    content_type="application/vnd.apache.parquet",
                )
    # Set equality in both directions, matching the local reader: a dataset manifest
    # holding cohorts the bundle does not publish makes the generation mean one thing on
    # its surface and another inside, and remote is where an alternate writer's store
    # would arrive.
    for name, manifest in manifests.items():
        if set(manifest.cohort_inventory) != set(bundle.cohorts):
            raise LakePublishError(
                f"remote calibration dataset publishes other cohorts than the bundle: {name}"
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
    publication: _RemotePublication,
    *,
    key: str,
    expected_sha256: str,
    model: type[ModelT],
) -> ModelT:
    payload = publication.require_small_object(
        key=key,
        expected_sha256=expected_sha256,
        content_type="application/json",
    )
    return load_lake_model_json(payload, model)


def _ensure_immutable(
    publication: _RemotePublication,
    *,
    key: str,
    path: Path,
    content_type: str,
    expected_sha256: str,
    expected_size: int,
) -> bool:
    if key in publication.verified:
        publication.require_identity(
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        )
        return False
    existing = publication.head(key)
    if existing is not None:
        publication.accept_existing(
            existing,
            key=key,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            content_type=content_type,
        )
        publication.reused_objects += 1
        return False
    with _sealed_upload(path) as (sealed, digest, content_md5, size):
        if digest != expected_sha256 or size != expected_size:
            raise LakePublishError(f"local upload source differs from graph identity: {key}")
        uploaded = True
        try:
            publication.put(
                key,
                sealed,
                sha256=digest,
                content_md5=content_md5,
                content_type=content_type,
                if_none_match=True,
            )
        except LakeCASConflict:
            uploaded = False
        publication.require_identity(
            key=key,
            expected_sha256=digest,
            expected_size=size,
            content_type=content_type,
            read_back=uploaded,
        )
        if uploaded:
            publication.uploaded_objects += 1
            publication.uploaded_bytes += size
        else:
            publication.reused_objects += 1
        return uploaded


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

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        result = self._run("get-object", "--key", key, str(path), payload_bytes=expect_bytes)
        assert result is not None

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
            self._run(*arguments, payload_bytes=path.stat().st_size)
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
        payload_bytes: int | None = None,
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
                timeout=_operation_timeout(payload_bytes),
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
    target.add_argument("--rollback-l1", action="store_true")
    parser.add_argument("--bucket", default="baibai-stores")
    parser.add_argument(
        "--verify-bytes",
        action="store_true",
        help=(
            "stream every reachable object back and hash it; this is the tamper audit, "
            "not the publication path, and it moves the whole closure"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = AwsCliR2Store(bucket=args.bucket)
    if args.rollback_calibration:
        rollback_report = rollback_calibration_bundle(store=store)
        print(json.dumps(rollback_report.as_dict(), sort_keys=True))
        return 0
    if args.rollback_l1:
        l1_rollback_report = rollback_l1_release(store=store)
        print(json.dumps(l1_rollback_report.as_dict(), sort_keys=True))
        return 0
    if args.mirror is None:
        parser.error("--mirror is required for publication")
    if args.raw_metadata is not None:
        raw_report = publish_raw_archive(
            mirror_root=args.mirror,
            metadata_path=args.raw_metadata,
            store=store,
            verify_bytes=args.verify_bytes,
        )
        print(json.dumps(raw_report.as_dict(), sort_keys=True))
        return 0
    if args.calibration_bundle is not None:
        bundle_report = publish_calibration_bundle(
            mirror_root=args.mirror,
            bundle_manifest_path=args.calibration_bundle,
            store=store,
            verify_bytes=args.verify_bytes,
        )
        print(json.dumps(bundle_report.as_dict(), sort_keys=True))
        return 0
    assert args.release_manifest is not None
    release_report = publish_l1_release(
        mirror_root=args.mirror,
        release_manifest_path=args.release_manifest,
        store=store,
        verify_bytes=args.verify_bytes,
    )
    print(json.dumps(release_report.as_dict(), sort_keys=True))
    return 0


def _operation_timeout(payload_bytes: int | None) -> int:
    """A deadline the object's own size can satisfy, not a constant it can outgrow."""
    if payload_bytes is None or payload_bytes <= 0:
        return _BASE_OPERATION_TIMEOUT_SECONDS
    transfer = -(-payload_bytes // _MIN_TRANSFER_BYTES_PER_SECOND)
    return _BASE_OPERATION_TIMEOUT_SECONDS + transfer


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
