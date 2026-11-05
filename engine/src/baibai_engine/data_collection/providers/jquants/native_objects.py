"""Integrity-checked conditional object writes shared by native J-Quants stores."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, ObjectStore


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read(store: ObjectStore, key: str) -> tuple[bytes, str] | None:
    before = store.head(key)
    if before is None:
        return None
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or before.etag != after.etag
        or len(payload) != before.size
        or _digest(payload) != before.metadata.get("sha256")
    ):
        raise LakePublishError("native L1 object changed or failed integrity")
    return payload, before.etag


def _put(
    store: ObjectStore, key: str, payload: bytes, directory: Path, *, etag: str | None
) -> None:
    """Retry the identical conditional write, never a newly generated receipt."""
    digest = _digest(payload)
    path = directory / (digest + ".object")
    path.write_bytes(payload)
    for attempt in range(2):
        try:
            store.put_file(
                key,
                path,
                sha256=digest,
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/octet-stream",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakePublishError:
            saved = _read(store, key)
            if saved is not None and saved[0] == payload:
                return
            if saved is not None and saved[1] != etag:
                raise LakeCASConflict("native L1 writer changed; retry from its head") from None
            if attempt:
                raise
        else:
            saved = _read(store, key)
            if saved is None or saved[0] != payload:
                raise LakePublishError("native L1 write outcome is unconfirmed")
            return
