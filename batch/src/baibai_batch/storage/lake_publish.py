"""Publish validated lake graphs to R2, switching their mutable pointer last by CAS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Never, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError
from pydantic import BaseModel

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import PutObjectRequestTypeDef

from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeDatasetManifest,
    LakeReleaseManifest,
    LakeSQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
    lake_current_l1_pointer_key,
    lake_dataset_manifest_key,
    lake_release_manifest_key,
    load_lake_model_json,
    validate_lake_release_policy,
)

_R2_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_POINTER_BYTES = 64 * 1024
_MAX_SMALL_OBJECT_BYTES = 16 * 1024 * 1024
# A per-attempt read timeout that is shorter than the transfer it guards turns a
# slow object into an avoidable retry. The floor covers control-plane latency; the
# transfer term is derived from the object's own size at a throughput well below
# what this link has measured (11.9 MB/s over the 2 GB store upload that preceded
# the lake, and 300 MB of Parquet in the first full release publication). Botocore's
# retry policy applies this value to each attempt; it is not an overall deadline.
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


@dataclass(frozen=True, slots=True)
class PointerPrecondition:
    """The exact mutable-pointer generation a publication started from."""

    etag: str | None


@dataclass(frozen=True, slots=True)
class PointerSnapshot:
    pointer: L1ReleasePointer | None
    precondition: PointerPrecondition


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


def read_pointer_snapshot(store: ObjectStore) -> PointerSnapshot:
    """Read and validate the exact mutable-pointer generation visible now."""

    publication = _RemotePublication(store=store)
    key = lake_current_l1_pointer_key()
    remote = publication.head(key)
    if remote is None:
        return PointerSnapshot(pointer=None, precondition=PointerPrecondition(etag=None))
    try:
        pointer = load_lake_model_json(publication.read_pointer(key, remote), L1ReleasePointer)
    except ValueError:
        raise LakePublishError("serving pointer is invalid") from None
    return PointerSnapshot(pointer=pointer, precondition=PointerPrecondition(etag=remote.etag))


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
    local_graph_seconds: float
    remote_closure_seconds: float
    pointer_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "pointer_etag": self.pointer_etag,
            **self.transfers.as_dict(),
        }


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


def publish_l1_release(
    *,
    mirror_root: Path,
    release_manifest_path: Path,
    store: ObjectStore,
    verify_bytes: bool = False,
    pointer_precondition: PointerPrecondition | None = None,
    expected_release_sha256: str | None = None,
) -> PublishReport:
    """Upload immutable graph nodes, then atomically switch the one mutable pointer."""
    publication = _RemotePublication(store=store, verify_bytes=verify_bytes)
    pointer_key = lake_current_l1_pointer_key()
    if pointer_precondition is None:
        initial = publication.head(pointer_key)
        if initial is not None:
            try:
                load_lake_model_json(
                    publication.read_pointer(pointer_key, initial), L1ReleasePointer
                )
            except ValueError:
                raise LakePublishError("serving pointer is invalid") from None
        pointer_precondition = PointerPrecondition(etag=None if initial is None else initial.etag)
    local_graph_started = time.perf_counter()
    root = mirror_root.resolve()
    resolved_release_path = release_manifest_path.resolve()
    if not resolved_release_path.is_relative_to(root):
        raise LakePublishError("release manifest path escapes mirror root")
    release_payload = resolved_release_path.read_bytes()
    release_sha256 = hashlib.sha256(release_payload).hexdigest()
    if expected_release_sha256 is not None and release_sha256 != expected_release_sha256:
        raise LakePublishError("release manifest changed after its publication was planned")
    release = load_lake_model_json(release_payload, LakeReleaseManifest)
    expected_release_key = lake_release_manifest_key(release_id=release.release_id)
    expected_release_path = _mirror_path(mirror_root, expected_release_key)
    if resolved_release_path != expected_release_path:
        raise LakePublishError("release manifest path does not match its release_id")

    uploads: list[tuple[str, Path, str, str, int]] = []
    manifests: dict[str, LakeDatasetManifest] = {}
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
                if not isinstance(source, LakeSQLiteSnapshotSourceRef):
                    raise LakePublishError("L1 graph contains an unsupported source reference")
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
            release_sha256,
            len(release_payload),
        )
    )

    validate_lake_release_policy(release, manifests)
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
    remote_closure_started = time.perf_counter()
    for key, path, content_type, expected_sha256, expected_size in uploads:
        _ensure_immutable(
            publication,
            key=key,
            path=path,
            content_type=content_type,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )

    pointer_started = time.perf_counter()
    current = publication.head(pointer_key)
    if current is not None:
        serving = load_lake_model_json(
            publication.read_pointer(pointer_key, current), L1ReleasePointer
        )
        # Republishing the same release is the retry of an interrupted publication, and
        # it has to be exactly the same release rather than the same name.
        if serving.release_id == release.release_id:
            if (
                serving.manifest_key != expected_release_key
                or serving.manifest_sha256 != release_sha256
            ):
                raise LakePublishError("current pointer reuses release ID with different identity")
            return PublishReport(
                release_id=release.release_id,
                transfers=publication.report(),
                pointer_etag=current.etag,
                local_graph_seconds=remote_closure_started - local_graph_started,
                remote_closure_seconds=pointer_started - remote_closure_started,
                pointer_seconds=time.perf_counter() - pointer_started,
            )
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=expected_release_key,
        manifest_sha256=release_sha256,
    )
    with tempfile.NamedTemporaryFile(prefix="baibai-l1-pointer-", suffix=".json") as temporary:
        temporary.write(canonical_lake_model_bytes(pointer))
        temporary.flush()
        pointer_payload = Path(temporary.name).read_bytes()
        try:
            result = publication.put(
                pointer_key,
                Path(temporary.name),
                sha256=hashlib.sha256(pointer_payload).hexdigest(),
                content_md5=_content_md5(Path(temporary.name)),
                content_type="application/json",
                if_match=pointer_precondition.etag,
                if_none_match=pointer_precondition.etag is None,
            )
            publication.require_pointer_bytes(key=pointer_key, expected=pointer_payload)
        except LakePublishError as exc:
            result = _reconcile_pointer_switch(
                publication,
                key=pointer_key,
                expected=pointer_payload,
                cause=exc,
            )
    return PublishReport(
        release_id=release.release_id,
        transfers=publication.report(),
        pointer_etag=result.etag,
        local_graph_seconds=remote_closure_started - local_graph_started,
        remote_closure_seconds=pointer_started - remote_closure_started,
        pointer_seconds=time.perf_counter() - pointer_started,
    )


def _reconcile_pointer_switch(
    publication: _RemotePublication,
    *,
    key: str,
    expected: bytes,
    cause: Exception,
) -> RemoteObject:
    """Resolve a conditional PUT whose remote commit result is ambiguous."""

    try:
        remote = publication.head(key)
        if remote is None:
            raise LakePublishError("L1 pointer is absent after an ambiguous switch")
        actual = publication.read_pointer(key, remote)
    except LakePublishError as reconciliation_error:
        raise LakePublishError(
            "L1 current pointer switch outcome is unknown; resolve current before retrying"
        ) from reconciliation_error
    if actual == expected:
        return remote
    raise LakeCASConflict("L1 current pointer moved to a different release") from cause


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


class Boto3R2Store:
    """S3-compatible R2 adapter with reusable in-process connections."""

    def __init__(self, *, bucket: str, env: Mapping[str, str] | None = None) -> None:
        source = dict(os.environ if env is None else env)
        account_id = _required(source, "R2_ACCOUNT_ID")
        if _R2_ACCOUNT_ID.fullmatch(account_id) is None:
            raise LakePublishError("R2_ACCOUNT_ID must be 32 lowercase hexadecimal characters")
        self.bucket = bucket
        self.endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self.access_key_id = _required(source, "R2_ACCESS_KEY_ID")
        self.secret_access_key = _required(source, "R2_SECRET_ACCESS_KEY")
        self._clients: dict[int, S3Client] = {}

    def _client(self, payload_bytes: int | None = None) -> S3Client:
        timeout = _operation_timeout(payload_bytes)
        cached = self._clients.get(timeout)
        if cached is not None:
            return cached
        client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name="auto",
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(
                connect_timeout=_BASE_OPERATION_TIMEOUT_SECONDS,
                read_timeout=timeout,
                retries={"mode": "standard", "total_max_attempts": 3},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self._clients[timeout] = client
        return client

    def head(self, key: str) -> RemoteObject | None:
        try:
            value = self._client().head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            _raise_r2_error("head-object", exc)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise LakePublishError("R2 head-object timed out") from exc
        except BotoCoreError as exc:
            raise LakePublishError("R2 head-object failed") from exc
        return RemoteObject(
            etag=str(value["ETag"]).strip('"'),
            size=int(value["ContentLength"]),
            metadata={str(k): str(v) for k, v in value.get("Metadata", {}).items()},
            content_type=str(value.get("ContentType", "application/octet-stream")),
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client().get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise LakePublishError("R2 get-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("get-object", exc)
        except BotoCoreError as exc:
            raise LakePublishError("R2 get-object failed") from exc

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            response = self._client(expect_bytes).get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                written = 0
                with temporary_path.open("wb") as target:
                    while chunk := body.read(8 * 1024 * 1024):
                        target.write(chunk)
                        written += len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                body.close()
            if expect_bytes is not None and written != expect_bytes:
                raise LakePublishError(
                    f"R2 get-object size mismatch: expected {expect_bytes}, got {written}"
                )
            temporary_path.replace(path)
            _fsync_directory(path.parent)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise LakePublishError("R2 get-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("get-object", exc)
        except BotoCoreError as exc:
            raise LakePublishError("R2 get-object failed") from exc
        finally:
            temporary_path.unlink(missing_ok=True)

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
        request: PutObjectRequestTypeDef = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": {
                "sha256": sha256,
                "content-md5": content_md5,
                "integrity": "content-md5-v1",
            },
            "ContentMD5": content_md5,
        }
        if if_match is not None:
            request["IfMatch"] = if_match
        if if_none_match:
            request["IfNoneMatch"] = "*"
        try:
            with path.open("rb") as body:
                request["Body"] = body
                self._client(path.stat().st_size).put_object(**request)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise LakePublishError("R2 put-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("put-object", exc)
        except BotoCoreError as exc:
            raise LakePublishError("R2 put-object failed") from exc
        result = self.head(key)
        if result is None:
            raise LakePublishError(f"R2 object missing after successful put: {key}")
        return result


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _raise_r2_error(operation: str, error: ClientError) -> Never:
    code = _error_code(error)
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"} or status in {
        409,
        412,
    }:
        raise LakeCASConflict(f"R2 conditional write conflict: {operation}") from error
    raise LakePublishError(f"R2 {operation} failed") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--release-manifest", type=Path, required=True)
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
    if args.mirror is None:
        parser.error("--mirror is required for publication")
    store = Boto3R2Store(bucket=args.bucket)
    try:
        payload = args.release_manifest.read_bytes()
        release = load_lake_model_json(payload, LakeReleaseManifest)
        release_sha256 = hashlib.sha256(payload).hexdigest()
        serving = read_pointer_snapshot(store)
        if serving.pointer is not None and (
            serving.pointer.release_id != release.release_id
            or serving.pointer.manifest_sha256 != release_sha256
        ):
            raise LakePublishError(
                "low-level publication cannot replace current; use publish_market_lake "
                "for a forward publication"
            )
        release_report = publish_l1_release(
            mirror_root=args.mirror,
            release_manifest_path=args.release_manifest,
            expected_release_sha256=release_sha256,
            store=store,
            verify_bytes=args.verify_bytes,
            pointer_precondition=serving.precondition,
        )
    except (OSError, ValueError, LakePublishError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(release_report.as_dict(), sort_keys=True))
    return 0


def _operation_timeout(payload_bytes: int | None) -> int:
    """A per-attempt read timeout the object's own size can satisfy."""
    if payload_bytes is None or payload_bytes <= 0:
        return _BASE_OPERATION_TIMEOUT_SECONDS
    transfer = -(-payload_bytes // _MIN_TRANSFER_BYTES_PER_SECOND)
    return _BASE_OPERATION_TIMEOUT_SECONDS + transfer


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
