"""Conditional small JSON objects, without interpreting source or workflow state."""

from __future__ import annotations

import base64
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.storage.objects.r2 import (
    CheckedObjectMetadataReader,
    LakePublishError,
    ObjectStore,
    VerifiedObjectMetadataReader,
)

MAX_PRIVATE_OBJECT_BYTES = 2_000_000


@dataclass(frozen=True)
class SavedObject:
    payload: bytes
    etag: str


def read_private(store: ObjectStore, key: str) -> SavedObject | None:
    # GET metadata belongs to that body; a final HEAD still detects a later version.
    if isinstance(store, CheckedObjectMetadataReader):
        response = store.get_checked_object(key, max_bytes=MAX_PRIVATE_OBJECT_BYTES)
        if response is None:
            return None
        payload, before = response
    else:
        previous = store.head(key)
        if previous is None:
            return None
        before = previous
        if not before.etag or not 0 < before.size <= MAX_PRIVATE_OBJECT_BYTES:
            raise LakePublishError("invalid private JSON object")
        payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or not before.etag
        or not 0 < before.size <= MAX_PRIVATE_OBJECT_BYTES
        or after.etag != before.etag
        or after.size != before.size
        or after.metadata.get("sha256") != before.metadata.get("sha256")
        or len(payload) != before.size
        or before.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise LakePublishError("private JSON object changed or failed integrity")
    return SavedObject(payload, before.etag)


def read_write_once_private(store: ObjectStore, key: str) -> SavedObject | None:
    """Read a create-only JSON object, checking the body and metadata from one response."""
    if isinstance(store, CheckedObjectMetadataReader):
        response = store.get_checked_object(key, max_bytes=MAX_PRIVATE_OBJECT_BYTES)
        return None if response is None else SavedObject(response[0], response[1].etag)
    return read_private(store, key)


def read_fixed_private(store: ObjectStore, key: str, *, sha256: str) -> SavedObject | None:
    """Read content-addressed JSON; mutable markers must use read_private instead."""
    if isinstance(store, VerifiedObjectMetadataReader):
        response = store.get_verified_object(key, sha256=sha256, max_bytes=MAX_PRIVATE_OBJECT_BYTES)
        saved = SavedObject(response[0], response[1].etag) if response is not None else None
    else:
        saved = read_private(store, key)
    if saved is not None and hashlib.sha256(saved.payload).hexdigest() != sha256:
        raise LakePublishError("fixed private JSON failed integrity")
    return saved


def put_private(
    store: ObjectStore, key: str, payload: bytes, *, previous_etag: str | None
) -> SavedObject:
    if not 0 < len(payload) <= MAX_PRIVATE_OBJECT_BYTES:
        raise ValueError("invalid private JSON payload size")
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="baibai-private-json-") as temporary:
        path = Path(temporary) / "payload.json"
        path.write_bytes(payload)
        path.chmod(0o600)
        result = store.put_file(
            key,
            path,
            sha256=digest,
            content_md5=base64.b64encode(
                hashlib.md5(payload, usedforsecurity=False).digest()
            ).decode(),
            content_type="application/json",
            if_match=previous_etag,
            if_none_match=previous_etag is None,
        )
    if not result.etag or result.size != len(payload) or result.metadata.get("sha256") != digest:
        raise LakePublishError("private JSON write receipt mismatch")
    return SavedObject(payload, result.etag)
