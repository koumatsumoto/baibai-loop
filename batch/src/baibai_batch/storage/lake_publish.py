"""Publish a validated local L1 release graph to R2, switching current last by CAS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeDatasetManifest,
    LakeRawArchiveMetadata,
    LakeReleaseManifest,
    canonical_json_bytes,
    lake_current_l1_pointer_key,
    lake_dataset_manifest_key,
    lake_release_manifest_key,
)

_R2_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_LEGACY_SOURCE_PREFIX = "legacy-sqlite-v"


class LakePublishError(RuntimeError):
    pass


class LakeCASConflict(LakePublishError):
    pass


@dataclass(frozen=True)
class RemoteObject:
    etag: str
    size: int
    metadata: Mapping[str, str]


class ObjectStore(Protocol):
    def head(self, key: str) -> RemoteObject | None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
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
    metadata = LakeRawArchiveMetadata.model_validate_json(metadata_path.read_bytes())
    object_path = _mirror_path(mirror_root, metadata.object_key)
    expected_metadata_path = Path(f"{object_path}.metadata.json")
    if metadata_path != expected_metadata_path:
        raise LakePublishError("Raw metadata path does not match its object_key")
    if _sha256(object_path) != metadata.content_sha256:
        raise LakePublishError("Raw object checksum does not match its metadata")
    if object_path.stat().st_size != metadata.bytes:
        raise LakePublishError("Raw object size does not match its metadata")
    uploads = (
        (metadata.object_key, object_path, "application/octet-stream"),
        (f"{metadata.object_key}.metadata.json", metadata_path, "application/json"),
    )
    uploaded = 0
    reused = 0
    for key, path, content_type in uploads:
        if _ensure_immutable(store, key=key, path=path, content_type=content_type):
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
    release = LakeReleaseManifest.model_validate_json(release_manifest_path.read_bytes())
    expected_release_key = lake_release_manifest_key(release_id=release.release_id)
    if release_manifest_path != mirror_root / expected_release_key:
        raise LakePublishError("release manifest path does not match its release_id")

    uploads: list[tuple[str, Path, str]] = []
    referenced_ingests: dict[tuple[str, str], tuple[Path, LakeRawArchiveMetadata]] = {}
    for dataset_name, release_dataset in sorted(release.datasets.items()):
        manifest_key = lake_dataset_manifest_key(
            dataset=dataset_name,
            build_id=release_dataset.build_id,
        )
        manifest_path = mirror_root / manifest_key
        manifest = LakeDatasetManifest.model_validate_json(manifest_path.read_bytes())
        if (
            manifest.dataset != dataset_name
            or manifest.build_id != release_dataset.build_id
            or manifest.contract_version != release_dataset.contract_version
        ):
            raise LakePublishError(f"release and dataset manifest disagree: {dataset_name}")
        for source_id in manifest.source_ingest_ids:
            if source_id.startswith(_LEGACY_SOURCE_PREFIX):
                continue
            identity = (dataset_name, source_id)
            if identity not in referenced_ingests:
                referenced_ingests[identity] = _find_raw_metadata(
                    mirror_root,
                    ingest_id=source_id,
                    dataset=dataset_name,
                )
        for partition in manifest.partitions:
            for item in partition.objects:
                object_path = mirror_root / item.key
                if _sha256(object_path) != item.sha256 or object_path.stat().st_size != item.bytes:
                    raise LakePublishError(f"local object does not match manifest: {item.key}")
                uploads.append((item.key, object_path, "application/vnd.apache.parquet"))
        uploads.append((manifest_key, manifest_path, "application/json"))
    uploads.append((expected_release_key, release_manifest_path, "application/json"))

    for metadata_path, metadata in referenced_ingests.values():
        object_path = _mirror_path(mirror_root, metadata.object_key)
        if _sha256(object_path) != metadata.content_sha256:
            raise LakePublishError(f"referenced Raw object checksum mismatch: {metadata.ingest_id}")
        uploads.extend(
            (
                (metadata.object_key, object_path, "application/octet-stream"),
                (f"{metadata.object_key}.metadata.json", metadata_path, "application/json"),
            )
        )

    uploaded = 0
    reused = 0
    for key, path, content_type in uploads:
        if _ensure_immutable(store, key=key, path=path, content_type=content_type):
            uploaded += 1
        else:
            reused += 1

    pointer_key = lake_current_l1_pointer_key()
    current = store.head(pointer_key)
    previous_release_id = None
    if current is not None:
        previous = L1ReleasePointer.model_validate_json(store.get_bytes(pointer_key))
        if previous.release_id == release.release_id:
            return PublishReport(
                release_id=release.release_id,
                uploaded_objects=uploaded,
                reused_objects=reused,
                pointer_etag=current.etag,
            )
        previous_release_id = previous.release_id
    release_sha256 = _sha256(release_manifest_path)
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=expected_release_key,
        manifest_sha256=release_sha256,
        previous_release_id=previous_release_id,
    )
    with tempfile.NamedTemporaryFile(prefix="baibai-l1-pointer-", suffix=".json") as temporary:
        temporary.write(canonical_json_bytes(pointer))
        temporary.flush()
        try:
            result = store.put_file(
                pointer_key,
                Path(temporary.name),
                sha256=_sha256(Path(temporary.name)),
                content_type="application/json",
                if_match=current.etag if current is not None else None,
                if_none_match=current is None,
            )
        except LakeCASConflict:
            raise
        except Exception as exc:
            raise LakePublishError("L1 current pointer switch failed") from exc
    return PublishReport(
        release_id=release.release_id,
        uploaded_objects=uploaded,
        reused_objects=reused,
        pointer_etag=result.etag,
    )


def _ensure_immutable(
    store: ObjectStore,
    *,
    key: str,
    path: Path,
    content_type: str,
) -> bool:
    digest = _sha256(path)
    existing = store.head(key)
    if existing is not None:
        if existing.metadata.get("sha256") != digest or existing.size != path.stat().st_size:
            raise LakePublishError(f"immutable R2 key already differs: {key}")
        return False
    try:
        store.put_file(
            key,
            path,
            sha256=digest,
            content_type=content_type,
            if_none_match=True,
        )
    except LakeCASConflict:
        raced = store.head(key)
        if (
            raced is None
            or raced.metadata.get("sha256") != digest
            or raced.size != path.stat().st_size
        ):
            raise
        return False
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
            f"sha256={sha256}",
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
        result = subprocess.run(  # nosec B603
            command,
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )
        if result.returncode == 0:
            return result
        error = result.stderr
        if allow_missing and ("Not Found" in error or "404" in error or "NoSuchKey" in error):
            return None
        if "PreconditionFailed" in error or "412" in error:
            raise LakeCASConflict(f"R2 conditional write conflict: {operation}")
        raise LakePublishError(f"R2 {operation} failed with exit code {result.returncode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--release-manifest", type=Path)
    target.add_argument("--raw-metadata", type=Path)
    parser.add_argument("--bucket", default="baibai-stores")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AwsCliR2Store(bucket=args.bucket)
    if args.raw_metadata is not None:
        raw_report = publish_raw_archive(
            mirror_root=args.mirror,
            metadata_path=args.raw_metadata,
            store=store,
        )
        print(json.dumps(raw_report.__dict__, sort_keys=True))
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


def _mirror_path(mirror_root: Path, key: str) -> Path:
    root = mirror_root.resolve()
    path = (root / key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LakePublishError(f"lake object escapes mirror root: {key}") from exc
    return path


def _find_raw_metadata(
    mirror_root: Path,
    *,
    ingest_id: str,
    dataset: str,
) -> tuple[Path, LakeRawArchiveMetadata]:
    matches: list[tuple[Path, LakeRawArchiveMetadata]] = []
    raw_root = mirror_root / "lake" / "l1" / "raw" / "jquants" / dataset
    for path in raw_root.glob("ingest_date=*/*.metadata.json"):
        metadata = LakeRawArchiveMetadata.model_validate_json(path.read_bytes())
        if metadata.ingest_id == ingest_id and metadata.dataset == dataset:
            matches.append((path, metadata))
    if len(matches) != 1:
        raise LakePublishError(
            f"expected exactly one Raw metadata object for {dataset}/{ingest_id}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
