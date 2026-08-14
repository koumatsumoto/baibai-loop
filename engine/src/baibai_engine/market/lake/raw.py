"""Append-only Raw capture with metadata derived from the installed bytes."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

from .datasets import require_pilot_dataset
from .immutable import ImmutableInstallError, install_immutable_bytes, install_immutable_file
from .keys import raw_metadata_object_key, raw_object_key
from .models import (
    RawArchiveMetadata,
    RawIngestSourceRef,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from .sources import sha256_file


class RawRetentionClass(StrEnum):
    PRESERVE = "preserve"
    BUFFER = "buffer"


class RawArchiveError(RuntimeError):
    pass


def archive_raw_file(
    *,
    source_path: Path,
    mirror_root: Path,
    provider: str,
    dataset: str,
    ingest_id: str,
    suffix: str,
    retention_class: RawRetentionClass,
    retrieved_at: datetime | None = None,
    endpoint: str | None = None,
    request_start: date | None = None,
    request_end: date | None = None,
) -> tuple[Path, Path, RawArchiveMetadata]:
    """Capture one finalized source pass and atomically install Raw plus metadata."""
    if provider != "jquants":
        raise RawArchiveError("Phase 1 Raw archive accepts provider='jquants' only")
    require_pilot_dataset(dataset)
    if not source_path.is_file() or source_path.is_symlink():
        raise RawArchiveError("Raw source must be a finalized regular file")
    if request_start is not None and request_end is not None and request_start > request_end:
        raise RawArchiveError("request_start must not be after request_end")
    retrieved = (retrieved_at or datetime.now(UTC)).astimezone(UTC)
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    typed_suffix = cast(Literal[".json.gz", ".csv.gz", ".zip"], normalized_suffix)
    key = raw_object_key(
        provider=provider,
        dataset=dataset,
        ingest_date=retrieved.date(),
        ingest_id=ingest_id,
        suffix=typed_suffix,
    )
    target = _mirror_path(mirror_root, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    captured = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.capture")
    try:
        digest, size = _capture_once(source_path, captured)
        if size <= 0:
            raise RawArchiveError("Raw source object must not be empty")
        install_immutable_file(target, captured, expected_sha256=digest)
    except (OSError, ImmutableInstallError) as exc:
        if isinstance(exc, ImmutableInstallError):
            raise RawArchiveError(
                f"immutable Raw key already contains different bytes: {target}"
            ) from exc
        raise RawArchiveError(str(exc)) from exc
    finally:
        captured.unlink(missing_ok=True)

    metadata = RawArchiveMetadata(
        metadata_version=1,
        provider=provider,
        dataset=dataset,
        ingest_id=ingest_id,
        retrieved_at=retrieved,
        retention_class=retention_class.value,
        suffix=typed_suffix,
        endpoint=_safe_endpoint(endpoint),
        request_start=request_start,
        request_end=request_end,
        object_key=key,
        content_sha256=digest,
        bytes=size,
    )
    metadata_key = raw_metadata_object_key(raw_key=key)
    metadata_path = _mirror_path(mirror_root, metadata_key)
    payload = canonical_lake_model_bytes(metadata)
    try:
        install_immutable_bytes(
            metadata_path,
            payload,
            validate=lambda value: load_lake_model_json(value, RawArchiveMetadata),
        )
    except ImmutableInstallError as exc:
        raise RawArchiveError(str(exc)) from exc
    if sha256_file(target) != metadata.content_sha256 or target.stat().st_size != metadata.bytes:
        raise RawArchiveError("installed Raw object differs from captured metadata")
    return target, metadata_path, metadata


def raw_source_ref(metadata_path: Path) -> RawIngestSourceRef:
    """Load a strict sidecar and return its content-bound typed lineage reference."""
    payload = metadata_path.read_bytes()
    metadata = load_lake_model_json(payload, RawArchiveMetadata)
    expected_key = raw_metadata_object_key(raw_key=metadata.object_key)
    if metadata_path.name != Path(expected_key).name:
        raise RawArchiveError("Raw metadata path does not match object identity")
    if metadata.request_start is None or metadata.request_end is None:
        raise RawArchiveError("canonical Raw lineage requires an explicit request range")
    return RawIngestSourceRef(
        kind="raw_ingest",
        source_id=metadata.ingest_id,
        provider=metadata.provider,
        dataset=metadata.dataset,
        request_start=metadata.request_start,
        request_end=metadata.request_end,
        key=metadata.object_key,
        sha256=metadata.content_sha256,
        metadata_version=metadata.metadata_version,
        metadata_key=expected_key,
        metadata_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _capture_once(source_path: Path, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source_path.open("rb") as source:
        before = os.fstat(source.fileno())
        with target.open("xb") as captured:
            while chunk := source.read(8 * 1024 * 1024):
                captured.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            captured.flush()
            os.fsync(captured.fileno())
        after = os.fstat(source.fileno())
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or size != after.st_size:
        raise RawArchiveError("Raw source changed while it was being captured")
    return digest.hexdigest(), size


def _safe_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise RawArchiveError("endpoint must not contain credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _mirror_path(mirror_root: Path, key: str) -> Path:
    root = mirror_root.resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise RawArchiveError("Raw object escapes mirror root")
    return path
