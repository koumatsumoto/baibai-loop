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
from typing import TYPE_CHECKING
from uuid import uuid4

from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    R2Error,
    RemoteObject,
)

if TYPE_CHECKING:
    from baibai_engine.storage.snapshots.release import LakeReleasePointer


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
        self.delete_keys: list[str] = []

    def close(self) -> None:
        pass

    def delete_object(self, key: str) -> None:
        self.delete_keys.append(key)
        self.values.pop(key, None)

    def iter_objects(self, prefix: str):
        yield from (
            (key, len(value.body))
            for key, value in sorted(self.values.items())
            if key.startswith(prefix)
        )

    def head(self, key: str) -> RemoteObject | None:
        self.head_keys.append(key)
        value = self.values.get(key)
        if value is None:
            return None
        return RemoteObject(
            etag=value.etag,
            size=len(value.body),
            metadata=dict(value.metadata),
            content_type=value.content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        self.get_keys.append(key)
        return self.values[key].body

    def get_bytes_with_metadata(self, key: str) -> tuple[bytes, RemoteObject] | None:
        self.get_keys.append(key)
        value = self.values.get(key)
        if value is None:
            return None
        return value.body, RemoteObject(
            etag=value.etag,
            size=len(value.body),
            metadata=dict(value.metadata),
            content_type=value.content_type,
        )

    def download_file(
        self,
        key: str,
        path: Path,
        *,
        expect_bytes: int | None = None,
        expect_etag: str | None = None,
    ) -> None:
        self.get_keys.append(key)
        value = self.values[key]
        if expect_etag is not None and value.etag != expect_etag:
            raise LakeCASConflict("selected download generation changed")
        body = value.body
        if expect_bytes is not None and len(body) != expect_bytes:
            raise R2Error(f"R2 get-object size mismatch: expected {expect_bytes}, got {len(body)}")
        path.write_bytes(body)

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
            raise R2Error(f"R2 put-object failed: Content-MD5 does not match body: {key}")
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


class VerifiedMemoryR2Store(MemoryR2Store):
    """An atomic fixed-object GET, including its byte and digest bounds."""

    def get_verified_object(self, key: str, *, sha256: str, max_bytes: int):
        response = self.get_bytes_with_metadata(key)
        if response is None:
            return None
        payload, info = response
        if (
            not 0 < info.size <= max_bytes
            or info.metadata.get("sha256") != sha256
            or hashlib.sha256(payload).hexdigest() != sha256
        ):
            raise LakePublishError("fixture fixed receipt read failed")
        return payload, info


def seed_machine_current(
    store: MemoryR2Store,
    release: LakeReleasePointer,
    *,
    market: bytes = b"market metadata fixture",
    runs: bytes = b"runs metadata fixture",
) -> LakeReleasePointer:
    """Seed the real current wire for tests consuming only the member metadata."""
    from baibai_engine.storage.snapshots.keys import current_release_pointer_key
    from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
    from baibai_engine.storage.snapshots.release import (
        LakeReleasePointer,
        MachineMember,
        MachineMembers,
        MachineRole,
        machine_member_key,
    )

    chosen = uuid4().hex

    def member(role: MachineRole, body: bytes) -> MachineMember:
        key = machine_member_key(chosen, role)
        store.store_object(key, body)
        meta = store.head(key)
        assert meta is not None
        return MachineMember(key=key, etag=meta.etag, size=meta.size)

    pointer = LakeReleasePointer(
        pointer_version=2,
        release_id=release.release_id,
        manifest_key=release.manifest_key,
        manifest_sha256=release.manifest_sha256,
        machine=MachineMembers(market=member("market", market), runs=member("runs", runs)),
    )
    store.store_object(current_release_pointer_key(), canonical_lake_model_bytes(pointer))
    return pointer
