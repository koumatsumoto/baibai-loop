"""Conditional small JSON objects, without interpreting source or workflow state."""

from __future__ import annotations

import base64
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore

MAX_PRIVATE_OBJECT_BYTES = 2_000_000


@dataclass(frozen=True)
class SavedObject:
    payload: bytes
    etag: str


def read_private(store: ObjectStore, key: str) -> SavedObject | None:
    before = store.head(key)
    if before is None:
        return None
    if not before.etag or not 0 < before.size <= MAX_PRIVATE_OBJECT_BYTES:
        raise LakePublishError("invalid private JSON object")
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or after.etag != before.etag
        or len(payload) != before.size
        or before.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise LakePublishError("private JSON object changed or failed integrity")
    return SavedObject(payload, before.etag)


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
