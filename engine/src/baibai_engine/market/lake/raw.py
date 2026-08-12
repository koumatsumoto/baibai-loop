"""Append-only Raw archive writer with explicit retention metadata."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .datasets import require_pilot_dataset
from .keys import raw_object_key, validate_lake_object_key


class RawRetentionClass(StrEnum):
    PRESERVE = "preserve"
    BUFFER = "buffer"


class RawArchiveMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata_version: Literal[1] = 1
    provider: str
    dataset: str
    ingest_id: str
    retrieved_at: datetime
    retention_class: RawRetentionClass
    suffix: Literal[".json.gz", ".csv.gz", ".zip"]
    endpoint: str | None = None
    request_start: date | None = None
    request_end: date | None = None
    object_key: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_derived_object_key(self) -> RawArchiveMetadata:
        validate_lake_object_key(self.object_key)
        expected = raw_object_key(
            provider=self.provider,
            dataset=self.dataset,
            ingest_date=self.retrieved_at.date(),
            ingest_id=self.ingest_id,
            suffix=self.suffix,
        )
        if self.object_key != expected:
            raise ValueError("Raw object_key does not match its metadata identity")
        return self


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
    """Copy provider bytes without transformation; never overwrite a different payload."""
    if provider != "jquants":
        raise RawArchiveError("Phase 1 Raw archive accepts provider='jquants' only")
    require_pilot_dataset(dataset)
    if not source_path.is_file():
        raise RawArchiveError(f"Raw source file does not exist: {source_path}")
    if request_start is not None and request_end is not None and request_start > request_end:
        raise RawArchiveError("request_start must not be after request_end")
    retrieved = (retrieved_at or datetime.now(UTC)).astimezone(UTC)
    digest = _sha256(source_path)
    size = source_path.stat().st_size
    if size <= 0:
        raise RawArchiveError("Raw source object must not be empty")
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    key = raw_object_key(
        provider=provider,
        dataset=dataset,
        ingest_date=retrieved.date(),
        ingest_id=ingest_id,
        suffix=normalized_suffix,
    )
    target = mirror_root / key
    _install_immutable(source_path, target, expected_sha256=digest)
    metadata = RawArchiveMetadata(
        provider=provider,
        dataset=dataset,
        ingest_id=ingest_id,
        retrieved_at=retrieved,
        retention_class=retention_class,
        suffix=normalized_suffix,
        endpoint=_safe_endpoint(endpoint),
        request_start=request_start,
        request_end=request_end,
        object_key=key,
        content_sha256=digest,
        bytes=size,
    )
    metadata_path = Path(f"{target}.metadata.json")
    payload = _json_bytes(metadata)
    _write_immutable_bytes(metadata_path, payload)
    return target, metadata_path, metadata


def _safe_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise RawArchiveError("endpoint must not contain credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_immutable(source: Path, target: Path, *, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _sha256(target) != expected_sha256:
            raise RawArchiveError(f"immutable Raw key already contains different bytes: {target}")
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if _sha256(target) != expected_sha256:
            raise RawArchiveError(
                f"immutable Raw key already contains different bytes: {target}"
            ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _write_immutable_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as target:
            target.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RawArchiveError(f"immutable metadata key already differs: {path}") from None


def _json_bytes(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
