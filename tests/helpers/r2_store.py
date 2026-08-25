"""An in-memory stand-in for the R2 object store the lake publishes to.

The fake models the service, not just the interface: a conditional write conflicts,
a read returns what was written, and a body that does not match its declared
Content-MD5 is refused the way R2 refuses it. #995 came from a fake that accepted an
ETag the real service would never produce, so the shapes it will not accept are the
part worth keeping in one place.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from baibai_batch.storage.lake_publish import (
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
)


def _content_md5(body: bytes) -> str:
    """The transport checksum R2 validates a PUT against."""

    return base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()  # nosec B324


def _etag(body: bytes) -> str:
    return hashlib.md5(body, usedforsecurity=False).hexdigest()  # nosec B324


@dataclass
class StoredObject:
    body: bytes
    etag: str
    metadata: dict[str, str]
    content_type: str


class MemoryR2Store:
    """A `Boto3R2Store` substitute that records every request it answered."""

    def __init__(self) -> None:
        self.values: dict[str, StoredObject] = {}
        self.conflict_pointer = False
        self.get_keys: list[str] = []
        self.head_keys: list[str] = []
        self.put_keys: list[str] = []

    def head(self, key: str) -> RemoteObject | None:
        self.head_keys.append(key)
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

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.get_keys.append(key)
        path.write_bytes(self.values[key].body)

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
        self.put_keys.append(key)
        current = self.values.get(key)
        if "/pointers/" in f"/{key}" and self.conflict_pointer:
            raise LakeCASConflict("injected")
        if if_none_match and current is not None:
            raise LakeCASConflict("exists")
        if if_match is not None and (current is None or current.etag != if_match):
            raise LakeCASConflict("stale")
        body = path.read_bytes()
        if content_md5 != _content_md5(body):
            # R2 answers BadDigest, which `Boto3R2Store.put_file` turns into this.
            raise LakePublishError(f"R2 put-object failed: Content-MD5 does not match body: {key}")
        self.store_object(key, body, content_type=content_type, sha256=sha256)
        result = self.head(key)
        assert result is not None
        return result

    def store_object(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/json",
        sha256: str | None = None,
    ) -> None:
        """Place bytes at `key` as a completed upload would leave them.

        Tests that need a remote object the publisher did not write — a pointer from
        another generation, an object tampered with in place — use this so the stored
        metadata stays the shape the service produces.
        """

        self.values[key] = StoredObject(
            body=body,
            etag=_etag(body),
            metadata={
                "sha256": sha256 if sha256 is not None else hashlib.sha256(body).hexdigest(),
                "content-md5": _content_md5(body),
                "integrity": "content-md5-v1",
            },
            content_type=content_type,
        )
