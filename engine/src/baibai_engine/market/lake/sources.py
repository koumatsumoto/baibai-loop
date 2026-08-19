"""Resolution and digest validation for typed lake lineage references."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .keys import dataset_manifest_key
from .models import (
    DatasetManifest,
    L1ReleaseSourceRef,
    ReleaseManifest,
    RetainedSourceRef,
    load_lake_model_json,
)

_VERIFIED: ContextVar[dict[tuple[str, str], Path] | None] = ContextVar(
    "lake_verified_sources", default=None
)


@contextmanager
def verified_source_scope() -> Iterator[None]:
    """Verify each immutable source once for the length of one operation.

    A retained source is named by every partition built from it, so verifying per
    reference makes the work scale with how many times a source is mentioned rather than
    with how much of it there is. A release that carries 121 months would re-hash the
    same objects once per partition that names them.

    What makes memoizing safe is what makes the reference worth verifying at all — the
    bytes are immutable and content addressed, so a source that verified at the start of
    the operation is the same source at the end. Two references that differ in any field
    are still verified separately: the entry is keyed by the whole reference the caller
    asserted, so a memo hit means this exact question was already answered.
    """

    token = _VERIFIED.set({})
    try:
        yield
    finally:
        _VERIFIED.reset(token)


def resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    """Resolve one retained source inside the mirror and verify its immutable identity.

    Only sources the lake stores can be resolved. An identity-only reference such as a
    sealed SQLite generation names no key, so it is excluded by type rather than by a
    runtime branch that would otherwise have to decide what a missing file means.
    """

    memo = _VERIFIED.get()
    # The whole reference, not the part of it that names a generation. Two references
    # can agree on kind, id, and digest and still ask different questions, so an empty
    # payload reused under two namespaces would otherwise let one verified reference
    # stand for another whose object is not there at all.
    identity = (str(mirror_root.resolve()), source.model_dump_json())
    if memo is not None and identity in memo:
        return memo[identity]
    path = _resolve_source_ref(mirror_root, source)
    if memo is not None:
        memo[identity] = path
    return path


def _resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    root = mirror_root.resolve()
    path = _resolve_key(root, mirror_root, source.key, source.sha256)
    if isinstance(source, L1ReleaseSourceRef):
        _require_release_closure(root, mirror_root, path)
    return path


def _resolve_key(root: Path, mirror_root: Path, key: str, sha256: str) -> Path:
    path = (mirror_root / key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("source reference does not resolve inside the lake mirror")
    if sha256_file(path) != sha256:
        raise ValueError("source reference digest does not match")
    return path


def _require_release_closure(root: Path, mirror_root: Path, manifest_path: Path) -> None:
    """Walk the whole graph the release roots, not just the manifest that names it.

    A release manifest is a list of dataset manifests, and each of those is a list of
    Parquet objects. Verifying only the root would let a cohort state a lineage whose
    rows are not in the mirror at all — the difference between "the reference is
    well-formed" and "the bytes it names can be read again", which is the entire
    distinction ``source_assurance`` draws.

    The objects are checked by digest rather than by presence and size. A cohort states
    this once per build, and the whole point of the claim is that these exact bytes are
    what a re-derivation would read.
    """

    try:
        release = load_lake_model_json(manifest_path.read_bytes(), ReleaseManifest)
    except ValueError as error:
        raise ValueError(f"l1_release source is not a release manifest: {error}") from error
    for name, entry in sorted(release.datasets.items()):
        dataset_path = _resolve_key(
            root,
            mirror_root,
            dataset_manifest_key(dataset=name, build_id=entry.build_id),
            entry.manifest_sha256,
        )
        try:
            manifest = load_lake_model_json(dataset_path.read_bytes(), DatasetManifest)
        except ValueError as error:
            raise ValueError(f"l1_release dataset manifest is invalid: {name}: {error}") from error
        for partition in manifest.partitions:
            for item in partition.objects:
                _resolve_key(root, mirror_root, item.key, item.sha256)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite_snapshot(path: Path, *, expected_schema_version: int) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError("SQLite source snapshot failed quick_check")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != expected_schema_version:
            raise ValueError("SQLite source snapshot schema version does not match")
