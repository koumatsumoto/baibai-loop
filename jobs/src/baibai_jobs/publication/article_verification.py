"""Read back only this integration's article receipt/projection/artifact closure."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.receipts.articles.snapshot_contracts import ARTICLE_RECEIPTS
from baibai_engine.data_collection.receipts.articles.store import read_receipt
from baibai_engine.machine_analysis.observations.articles.snapshot_contracts import (
    ARTICLE_EXTRACTION_ARTIFACTS,
    ARTICLE_OBSERVATIONS,
    ARTICLE_PROJECTION_RUNS,
)
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import accepted_dataset, materialize_partitions
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.publication.snapshot_publication import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)

DATASETS = (
    ARTICLE_RECEIPTS,
    ARTICLE_PROJECTION_RUNS,
    ARTICLE_OBSERVATIONS,
    ARTICLE_EXTRACTION_ARTIFACTS,
)


def _digest(row: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in row:
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def capture_delta(
    database: Path, receipt_ids: Sequence[str], *, artifact_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """Freeze the affected closure after native projection and semantic extraction."""
    result = {}
    with closing(connect_read_only(database)) as conn:
        selected = set(receipt_ids)
        for artifact_id in artifact_ids:
            row = conn.execute(
                "SELECT o.receipt_id FROM article_extraction_artifacts a "
                "JOIN article_observations o ON o.observation_id=a.observation_id "
                "WHERE a.artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ValueError("selected article extraction artifact is missing")
            selected.add(row[0])
        for identity in tuple(selected):
            receipt = read_receipt(conn, identity)
            if receipt.body_ref is not None:
                selected.add(receipt.body_ref.receipt_id)
        for dataset in DATASETS:
            rows: list[sqlite3.Row] = []
            columns = ",".join(column.name for column in dataset.columns)
            for receipt_id in sorted(selected):
                if dataset == ARTICLE_EXTRACTION_ARTIFACTS:
                    where = (
                        "observation_id IN (SELECT observation_id FROM article_observations "
                        "WHERE receipt_id=?)"
                    )
                else:
                    where = "receipt_id=?"
                rows.extend(
                    conn.execute(
                        f"SELECT {columns} FROM {dataset.sqlite_table} WHERE {where}",  # nosec B608
                        (receipt_id,),
                    )
                )
            names = [column.name for column in dataset.columns]
            day_index = names.index(dataset.date_column)
            result[dataset.name] = {
                row[0]: {"sha256": _digest(tuple(row)), "date": row[day_index]} for row in rows
            }
    if set(result[ARTICLE_RECEIPTS.name]) != selected:
        raise ValueError("article delta is missing an applied original receipt")
    return result


def verify_delta(
    store: ObjectStore, *, delta: dict[str, Any], directory: Path, generations: dict[str, str]
) -> dict[str, int | float]:
    started = time.monotonic()
    pointer = _serving_pointer(store).pointer
    if (
        pointer is None
        or pointer.release_id != generations["lake_release_id"]
        or pointer.manifest_sha256 != generations["lake_manifest_sha256"]
    ):
        raise LakePublishError("article readback differs from the fixed publication")
    directory.mkdir(parents=True, exist_ok=True)
    release = _resolve_serving_release(store, directory, pointer)
    cache = LakeObjectCache(root=directory, source=LocalMirrorSource(directory))
    counts: dict[str, int | float] = {}
    with lake_session() as session:
        for dataset in DATASETS:
            expected = delta.get(dataset.name, {})
            if not expected:
                counts[dataset.name] = 0
                continue
            accepted_dataset(release, dataset)
            months = {
                (date.fromisoformat(item["date"]).year, date.fromisoformat(item["date"]).month)
                for item in expected.values()
            }
            parts = tuple(
                part
                for part in release.dataset_manifests[dataset.name].partitions
                if (part.values["year"], part.values["month"]) in months
            )
            for part in parts:
                for obj in part.objects:
                    _fetch(store, directory, obj.key, expected_sha256=obj.sha256)
            paths = materialize_partitions(cache, dataset=dataset, partitions=parts)
            if not paths:
                raise LakePublishError("article delta publication partition is missing")
            columns = ",".join(column.name for column in dataset.columns)
            key = dataset.primary_key[0]
            actual = session.connection.execute(
                f"SELECT {columns} FROM read_parquet($files) WHERE {key} IN (SELECT unnest($ids))",  # nosec B608
                {"files": [str(path) for path in paths], "ids": list(expected)},
            ).fetchall()
            if {row[0]: _digest(row) for row in actual} != {
                identity: value["sha256"] for identity, value in expected.items()
            }:
                raise LakePublishError("article raw/extraction delta readback differs")
            counts[dataset.name] = len(actual)
    counts["readback_seconds"] = round(time.monotonic() - started, 3)
    return counts
