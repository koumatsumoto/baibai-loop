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
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.snapshots.duck import LakeSession, lake_session
from baibai_engine.storage.snapshots.keys import validate_lake_object_key
from baibai_engine.storage.snapshots.models import LakeObject
from baibai_engine.storage.snapshots.sources import sha256_file

_GLOB_METACHARACTERS = frozenset("*?[]{}")
# The first fresh release contains a 4 GB archive object. Fetching that object as
# one Python bytes value (or holding it in a prefetch slot) exhausts a hosted runner.
MAX_BUFFERED_OBJECT_BYTES = 8 * 1024 * 1024
_STREAM_CHUNK_BYTES = 8 * 1024 * 1024
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_ACCESS_KEY_ID = re.compile(r"^[A-Za-z0-9]{16,128}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class LakeCredentialError(RuntimeError):
    """R2 read credentials are absent or do not have the expected shape."""


@dataclass(frozen=True, slots=True)
class R2ReadCredentials:
    """Snapshot read configuration, frozen before any worker starts."""

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    def __post_init__(self) -> None:
        if _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise LakeCredentialError("R2_ACCOUNT_ID must be 32 lowercase hexadecimal characters")
        if _ACCESS_KEY_ID.fullmatch(self.access_key_id) is None:
            raise LakeCredentialError("R2_ACCESS_KEY_ID must be an alphanumeric access key id")
        if not self.secret_access_key:
            raise LakeCredentialError("R2_SECRET_ACCESS_KEY must not be empty")
        if _BUCKET.fullmatch(self.bucket) is None:
            raise LakeCredentialError("R2 bucket must be a lowercase DNS-safe name")

    def __repr__(self) -> str:
        return f"R2ReadCredentials(bucket={self.bucket!r})"

    __str__ = __repr__

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, bucket: str = "baibai-stores"
    ) -> R2ReadCredentials:
        source = os.environ if env is None else env
        return cls(
            account_id=_required(source, "R2_ACCOUNT_ID"),
            access_key_id=_required(source, "R2_ACCESS_KEY_ID"),
            secret_access_key=_required(source, "R2_SECRET_ACCESS_KEY"),
            bucket=bucket,
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise LakeCredentialError(f"required environment variable is missing: {name}")
    return value


class LakeObjectError(RuntimeError):
    """An object is missing, unreadable, or does not match its manifest identity."""


class LakeObjectSource(Protocol):
    """Read one object by its exact key."""

    def read_bytes(self, key: str) -> bytes: ...


class _Downloader(Protocol):
    def __call__(self, key: str, destination: Path, *, max_bytes: int) -> None: ...


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

    def download_file(self, key: str, destination: Path, *, max_bytes: int) -> None:
        try:
            with self.path(key).open("rb") as source, destination.open("xb") as target:
                size = 0
                while chunk := source.read(_STREAM_CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_bytes:
                        raise LakeObjectError(f"lake object exceeds manifest size: {key}")
                    target.write(chunk)
        except OSError as exc:
            raise LakeObjectError(f"lake object is not readable: {key}") from exc

    def path(self, key: str) -> Path:
        return mirror_path(self.root, key)


class R2ObjectSource:
    """Exact snapshot keys transferred by the shared R2 object store."""

    def __init__(self, credentials: R2ReadCredentials) -> None:
        self._store = Boto3R2Store(
            bucket=credentials.bucket,
            env={
                "R2_ACCOUNT_ID": credentials.account_id,
                "R2_ACCESS_KEY_ID": credentials.access_key_id,
                "R2_SECRET_ACCESS_KEY": credentials.secret_access_key,
            },
        )

    def close(self) -> None:
        self._store.close()

    def read_bytes(self, key: str) -> bytes:
        validate_lake_object_key(key)
        try:
            return self._store.get_bytes(key)
        except Exception:
            # Provider exceptions can contain credentials or the account endpoint.
            raise LakeObjectError(f"lake object is not readable: {key}") from None

    def download_file(self, key: str, destination: Path, *, max_bytes: int) -> None:
        """Stream one exact R2 key to disk without materializing its body in memory."""
        validate_lake_object_key(key)
        try:
            self._store.download_file(key, destination, expect_bytes=max_bytes, max_bytes=max_bytes)
        except Exception:
            raise LakeObjectError(f"lake object is not readable: {key}") from None


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
    evict_large_after_read: bool = False

    def retain_metadata(self, key: str, payload: bytes, *, sha256: str) -> Path:
        """Keep already-resolved immutable metadata beside its verified Parquet objects."""
        if sha256_bytes(payload) != sha256:
            raise LakeObjectError("resolved lake metadata digest differs")
        path = mirror_path(self.root, key)
        if not path.is_file() or sha256_file(path) != sha256:
            _install(path, payload)
        return path

    def materialize(self, lake_object: LakeObject) -> Path:
        path = mirror_path(self.root, lake_object.key)
        if path.is_file() and self._cached_identity_holds(path, lake_object):
            self.transfers.reused_objects += 1
            self.transfers.reused_bytes += lake_object.bytes
            return path
        downloader = getattr(self.source, "download_file", None)
        if lake_object.bytes > MAX_BUFFERED_OBJECT_BYTES and downloader is not None:
            _install_stream(path, lake_object, downloader)
        else:
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


def _install_stream(path: Path, lake_object: LakeObject, downloader: _Downloader) -> None:
    """Install only after the streamed file matches the manifest byte for byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    try:
        downloader(lake_object.key, temporary, max_bytes=lake_object.bytes)
        if (
            temporary.stat().st_size != lake_object.bytes
            or sha256_file(temporary) != lake_object.sha256
        ):
            raise LakeObjectError(
                f"lake object digest differs from its manifest: {lake_object.key}"
            )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
    also the only place objects can come from — an offline build cannot silently
    reach the network. DuckDB only queries verified local files. With ``bucket`` the
    same directory becomes a fetch-through cache, so a partition that an earlier
    release already materialized costs nothing to read again.
    """

    mirror_root(mirror)
    credentials = None if bucket is None else R2ReadCredentials.from_env(env, bucket=bucket)
    with lake_session() as session:
        if credentials is None:
            yield session, LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror))
        else:
            with closing(R2ObjectSource(credentials)) as source:
                yield session, LakeObjectCache(root=mirror, source=source)
