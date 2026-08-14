"""Resolution and digest validation for typed lake lineage references."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .models import (
    L1ReleaseSourceRef,
    RawArchiveMetadata,
    RawIngestSourceRef,
    ReleaseManifest,
    SourceRef,
    SQLiteSnapshotSourceRef,
    load_lake_model_json,
)


def resolve_source_ref(mirror_root: Path, source: SourceRef) -> Path:
    """Resolve one source inside the mirror and verify its immutable identity."""
    root = mirror_root.resolve()
    path = (mirror_root / source.key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("source reference does not resolve inside the lake mirror")
    if sha256_file(path) != source.sha256:
        raise ValueError("source reference digest does not match")
    if isinstance(source, RawIngestSourceRef):
        _validate_raw_metadata(mirror_root, source)
    elif isinstance(source, SQLiteSnapshotSourceRef):
        _validate_sqlite_snapshot(path, expected_schema_version=source.schema_version)
    elif isinstance(source, L1ReleaseSourceRef):
        manifest = load_lake_model_json(path.read_bytes(), ReleaseManifest)
        if (
            manifest.release_id != source.source_id
            or manifest.manifest_version != source.manifest_version
        ):
            raise ValueError("L1 release source identity does not match")
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


def _validate_sqlite_snapshot(path: Path, *, expected_schema_version: int) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError("SQLite source snapshot failed quick_check")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != expected_schema_version:
            raise ValueError("SQLite source snapshot schema version does not match")
