"""Prove a disposable native projection against its previously published rows."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.models import DatasetManifest
from baibai_engine.storage.snapshots.objects import LakeObjectError, mirror_path
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import LakeBuildError, validate_snapshot_parity


def verified_cached_scopes(
    store: ObjectStore,
    previous: DatasetManifest | None,
    *,
    market: Path,
    mirror_root: Path,
    dataset: LakeDataset,
    namespace: str,
    input_format: str,
    read_scopes: Callable[[dict[str, JsonValue]], dict[str, Any]],
) -> dict[str, Any]:
    """Reuse only a complete native projection whose Parquet and SQLite agree.

    A missing or damaged prior projection is optional: the source caller falls
    back to its fixed L1. Counts and mutable SQLite coverage are not cache proof.
    """
    if previous is None or previous.coverage_status != "complete":
        return {}
    sources = {source for part in previous.partitions for source in part.sources}
    if len(sources) != 1:
        return {}
    source = next(iter(sources))
    if source.kind != "native_l1" or source.namespace != namespace:
        return {}
    descriptor: dict[str, JsonValue] = {"format": input_format, "selection_sha256": source.sha256}
    try:
        for part in previous.partitions:
            for obj in part.objects:
                path = mirror_path(mirror_root, obj.key)
                if not path.is_file() or sha256_file(path) != obj.sha256:
                    payload = store.get_bytes(obj.key)
                    if (
                        len(payload) != obj.bytes
                        or hashlib.sha256(payload).hexdigest() != obj.sha256
                    ):
                        return {}
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
        validate_snapshot_parity(
            dataset=dataset,
            sqlite_path=market,
            mirror_root=mirror_root,
            manifest=previous,
        )
        return read_scopes(descriptor)
    except (
        KeyError,
        ValueError,
        OSError,
        sqlite3.DatabaseError,
        LakeBuildError,
        LakeObjectError,
        LakePublishError,
    ):
        return {}
