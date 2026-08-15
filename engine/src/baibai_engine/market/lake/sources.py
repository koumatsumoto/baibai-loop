"""Resolution and digest validation for typed lake lineage references."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .models import (
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    RawArchiveMetadata,
    RawIngestSourceRef,
    RetainedSourceRef,
    load_lake_model_json,
)


def resolve_source_ref(mirror_root: Path, source: RetainedSourceRef) -> Path:
    """Resolve one retained source inside the mirror and verify its immutable identity.

    Only sources the lake stores can be resolved. An identity-only reference such as a
    sealed SQLite generation names no key, so it is excluded by type rather than by a
    runtime branch that would otherwise have to decide what a missing file means.
    """

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
