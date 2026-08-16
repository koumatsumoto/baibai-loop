"""Reading immutable lake objects by explicit key, never by discovery.

Every read names the key the manifest published. Nothing here lists a prefix,
expands a glob, or asks the store for "the latest" object: the key allowlist in
``keys`` rejects the glob metacharacters outright, so an object either has the key
a manifest fixed or it is not read at all.

``LakeObjectCache`` is the transfer boundary. Object keys are content addressed, so
a partition that did not change between two releases resolves to a file that is
already on disk and costs no bytes. Every byte that is used is verified against the
manifest digest first, whether it arrived now or in an earlier build.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import duckdb

from .duck import LakeSession, R2ReadCredentials, lake_session, r2_uri
from .keys import validate_lake_object_key
from .models import LakeObject

_READ_CHUNK = 8 * 1024 * 1024
_GLOB_METACHARACTERS = frozenset("*?[]{}")


class LakeObjectError(RuntimeError):
    """An object is missing, unreadable, or does not match its manifest identity."""


class LakeObjectSource(Protocol):
    """Read one object by its exact key."""

    def read_bytes(self, key: str) -> bytes: ...

    def uri(self, key: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LocalMirrorSource:
    """A local mirror of the bucket namespace, rooted at one directory."""

    root: Path

    def read_bytes(self, key: str) -> bytes:
        path = self.path(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise LakeObjectError(f"lake object is not readable: {key}") from exc

    def uri(self, key: str) -> str:
        return str(self.path(key))

    def path(self, key: str) -> Path:
        return mirror_path(self.root, key)


@dataclass(frozen=True, slots=True)
class R2ObjectSource:
    """The R2 bucket, read through the session's authorised DuckDB connection."""

    session: LakeSession

    def read_bytes(self, key: str) -> bytes:
        uri = self.uri(key)
        try:
            rows = self.session.connection.execute(
                "SELECT content FROM read_blob($uri)", {"uri": uri}
            ).fetchall()
        except duckdb.Error as exc:
            raise LakeObjectError(
                f"lake object is not readable: {key}: {self.session.redact(str(exc))}"
            ) from None
        if len(rows) != 1 or not isinstance(rows[0][0], bytes):
            raise LakeObjectError(f"lake object did not resolve to exactly one payload: {key}")
        return rows[0][0]

    def uri(self, key: str) -> str:
        if self.session.credentials is None:
            raise LakeObjectError("R2 object source requires an authorised session")
        return r2_uri(self.session.credentials, validate_lake_object_key(key))


def mirror_path(root: Path, key: str) -> Path:
    """Resolve a validated key below ``root`` and refuse anything that escapes it."""

    validate_lake_object_key(key)
    base = mirror_root(root)
    path = (base / key).resolve()
    if path != base and base not in path.parents:
        raise LakeObjectError(f"lake object key escapes its root: {key}")
    return path


def mirror_root(root: Path) -> Path:
    """Resolve the mirror root, refusing one whose name would be read as a pattern.

    Object keys are pattern-free by allowlist, but what reaches ``read_parquet`` is
    the root joined to the key. DuckDB expands each path it is given as a glob, so a
    root containing a metacharacter would turn an exact file list back into a search
    — and the object verification, which reads the unexpanded path, would then be
    checking different files from the ones read.
    """

    base = root.resolve()
    if _GLOB_METACHARACTERS.intersection(str(base)):
        raise LakeObjectError(f"lake mirror root must not contain glob metacharacters: {base}")
    return base


@dataclass(slots=True)
class TransferAccounting:
    """Bytes and objects this build had to move, and what it reused."""

    fetched_objects: int = 0
    fetched_bytes: int = 0
    reused_objects: int = 0
    reused_bytes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched_objects": self.fetched_objects,
            "fetched_bytes": self.fetched_bytes,
            "reused_objects": self.reused_objects,
            "reused_bytes": self.reused_bytes,
        }

    def since(self, before: Mapping[str, int]) -> TransferAccounting:
        """What moved after ``before`` was taken.

        The cache accumulates across every build it serves, so a report that showed
        the live counters would attribute an earlier build's transfer to this one —
        and the transfer split is the only evidence that unchanged partitions cost
        nothing.
        """

        return TransferAccounting(
            fetched_objects=self.fetched_objects - before["fetched_objects"],
            fetched_bytes=self.fetched_bytes - before["fetched_bytes"],
            reused_objects=self.reused_objects - before["reused_objects"],
            reused_bytes=self.reused_bytes - before["reused_bytes"],
        )


@dataclass(slots=True)
class LakeObjectCache:
    """Content-addressed local copies of published objects.

    The cache root mirrors the bucket namespace, so a cached object sits at the key
    the manifest published and a second release that references the same partition
    finds it already present.
    """

    root: Path
    source: LakeObjectSource
    transfers: TransferAccounting = field(default_factory=TransferAccounting)

    def materialize(self, lake_object: LakeObject) -> Path:
        path = mirror_path(self.root, lake_object.key)
        if path.is_file() and self._cached_identity_holds(path, lake_object):
            self.transfers.reused_objects += 1
            self.transfers.reused_bytes += lake_object.bytes
            return path
        payload = self.source.read_bytes(lake_object.key)
        _require_payload_identity(payload, lake_object)
        _install(path, payload)
        self.transfers.fetched_objects += 1
        self.transfers.fetched_bytes += lake_object.bytes
        return path

    def _cached_identity_holds(self, path: Path, lake_object: LakeObject) -> bool:
        """Whether the cached copy is usable, discarding it when a refetch can heal it.

        The cache is derived, not authoritative, so a copy that no longer matches its
        manifest is a local fault rather than data loss. When the objects can be read
        from somewhere other than this directory, the damaged copy is dropped and the
        exact key is fetched again; a refetched copy that still differs stops the read,
        because that is corruption at the source. When the cache *is* the only source,
        refetching would read the same bytes back, so the read fails closed instead of
        looping.
        """

        if path.stat().st_size == lake_object.bytes and sha256_file(path) == lake_object.sha256:
            return True
        if isinstance(self.source, LocalMirrorSource) and mirror_root(
            self.source.root
        ) == mirror_root(self.root):
            raise LakeObjectError(f"cached lake object differs: {lake_object.key}")
        try:
            path.unlink()
        except OSError as exc:
            raise LakeObjectError(
                f"cached lake object differs and cannot be replaced: {lake_object.key}"
            ) from exc
        return False


def _require_payload_identity(payload: bytes, lake_object: LakeObject) -> None:
    if len(payload) != lake_object.bytes:
        raise LakeObjectError(f"lake object size differs from its manifest: {lake_object.key}")
    if hashlib.sha256(payload).hexdigest() != lake_object.sha256:
        raise LakeObjectError(f"lake object digest differs from its manifest: {lake_object.key}")


def _install(path: Path, payload: bytes) -> None:
    """Write through a unique temporary name so no reader sees a partial object."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def open_lake(
    *,
    mirror: Path,
    bucket: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Iterator[tuple[LakeSession, LakeObjectCache]]:
    """Open a session and the object cache rooted at the local mirror.

    ``mirror`` is always the local content-addressed cache. Without ``bucket`` it is
    also the only place objects can come from, and the session never loads the HTTP
    layer — an offline build cannot silently reach the network. With ``bucket`` the
    same directory becomes a fetch-through cache, so a partition that an earlier
    release already materialized costs nothing to read again.
    """

    mirror_root(mirror)
    credentials = None if bucket is None else R2ReadCredentials.from_env(env, bucket=bucket)
    with lake_session(credentials=credentials) as session:
        source: LakeObjectSource = (
            LocalMirrorSource(mirror) if credentials is None else R2ObjectSource(session)
        )
        yield session, LakeObjectCache(root=mirror, source=source)
