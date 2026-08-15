"""Resolution and digest validation for typed lake lineage references."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .models import (
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    RawArchiveMetadata,
    RawIngestSourceRef,
    RetainedSourceRef,
    load_lake_model_json,
)

_VERIFIED: ContextVar[dict[tuple[str, str, str, str], Path] | None] = ContextVar(
    "lake_verified_sources", default=None
)


@contextmanager
def verified_source_scope() -> Iterator[None]:
    """Verify each immutable source once for the length of one operation.

    A source is named by every cohort built from it, and one legacy archive can be named
    by every cohort in the store. Verifying per reference makes the work scale with how
    many times a generation is mentioned rather than with how much of it there is: 81
    cohorts over a 500 MB archive is hundreds of gigabytes of hashing for one migration.

    What makes memoizing safe is what makes the reference worth verifying at all — the
    bytes are immutable and content addressed, and the operation holds the writer lock,
    so a source that verified at the start of the operation is the same source at the
    end. Two references that claim the same identity but resolve differently are still
    caught: the entry is keyed by the identity the caller asserted.
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
    identity = (str(mirror_root.resolve()), source.kind, source.source_id, source.sha256)
    if memo is not None and identity in memo:
        return memo[identity]
    path = _resolve_source_ref(mirror_root, source)
    if memo is not None:
        memo[identity] = path
    return path


def _resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    root = mirror_root.resolve()
    path = (mirror_root / source.key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("source reference does not resolve inside the lake mirror")
    if sha256_file(path) != source.sha256:
        raise ValueError("source reference digest does not match")
    if isinstance(source, RawIngestSourceRef):
        _validate_raw_metadata(mirror_root, source)
    elif isinstance(source, CalibrationInputSourceRef):
        input_manifest = load_lake_model_json(path.read_bytes(), CalibrationInputManifest)
        if (
            input_manifest.input_id != source.source_id
            or input_manifest.manifest_version != source.manifest_version
        ):
            raise ValueError("calibration input source identity does not match")
        for item in input_manifest.files.values():
            archived = (mirror_root / item.key).resolve()
            if (
                not archived.is_relative_to(root)
                or not archived.is_file()
                or archived.stat().st_size != item.bytes
                or sha256_file(archived) != item.sha256
            ):
                raise ValueError("calibration input archive identity does not match")
    return path


def _validate_raw_metadata(mirror_root: Path, source: RawIngestSourceRef) -> None:
    root = mirror_root.resolve()
    path = (mirror_root / source.metadata_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Raw metadata reference does not resolve inside the lake mirror")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != source.metadata_sha256:
        raise ValueError("Raw metadata reference digest does not match")
    metadata = load_lake_model_json(payload, RawArchiveMetadata)
    if (
        metadata.metadata_version != source.metadata_version
        or metadata.provider != source.provider
        or metadata.dataset != source.dataset
        or metadata.request_start != source.request_start
        or metadata.request_end != source.request_end
        or metadata.ingest_id != source.source_id
        or metadata.object_key != source.key
        or metadata.content_sha256 != source.sha256
    ):
        raise ValueError("Raw metadata identity does not match source reference")


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
